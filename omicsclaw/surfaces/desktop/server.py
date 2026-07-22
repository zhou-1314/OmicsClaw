"""
server.py -- OmicsClaw desktop/web FastAPI backend
==================================================
Wraps omicsclaw.runtime.agent.state (OmicsClaw query engine) as a streaming HTTP API consumed by
the Electron and Next.js frontends.

Start:
    python -m omicsclaw.surfaces.desktop.server --host 127.0.0.1 --port 8765
    oc desktop-server --host 127.0.0.1 --port 8765

The server expects the OmicsClaw source tree to be importable, either from a
source checkout / editable install or from a Python environment where the
package has been installed.
"""

from __future__ import annotations

import asyncio
import argparse
import ast
import base64
import hmac
import importlib.util
import ipaddress
import json
import logging
import mimetypes
import os
import platform
import queue
import secrets
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Annotated, Any, MutableMapping, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    model_validator,
)
from starlette.background import BackgroundTask
from starlette.types import Scope

from omicsclaw.common.output_claim import (
    OUTPUT_CLAIM_FILENAME,
    atomic_write_owned_output_text,
    collect_output_claim_identities,
    is_contained_output_path,
    is_scientific_output_file,
)
from omicsclaw.autoagent.http_guard import AutoAgentStartBodyGuardMiddleware

try:
    from omicsclaw.surfaces.desktop.notebook.kernel_manager import get_kernel_manager
    from omicsclaw.surfaces.desktop.notebook.live_session import (
        install_live_session_support,
    )
    from omicsclaw.surfaces.desktop.notebook.router import router as notebook_router

    _NOTEBOOK_AVAILABLE = True
    _NOTEBOOK_IMPORT_ERROR: str = ""
except ImportError as _nb_err:
    # NOTE: ``_nb_err`` is scoped to this except block (Python 3 deletes
    # exception bindings on exit, PEP 3134). Persist the message to a
    # module-level variable so the lifespan log on startup can surface it.
    _NOTEBOOK_AVAILABLE = False
    _NOTEBOOK_IMPORT_ERROR = str(_nb_err)
    get_kernel_manager = None  # type: ignore[assignment]
    install_live_session_support = None  # type: ignore[assignment]
    notebook_router = None  # type: ignore[assignment]
from omicsclaw.runtime.policy.state import ToolPolicyState
from omicsclaw.control import (
    ControlRuntime,
    ControlRuntimePorts,
    RawContentBlockV1,
    RawInboundV1,
    RunAcceptanceStatus,
    TurnAcceptanceStatus,
)
from omicsclaw.control.run_runtime import RunRuntime
from omicsclaw.control.event_hub import EventHubCapacityError
from omicsclaw.remote.auth import (
    BearerGatePolicy,
    RemoteBearerMiddleware,
    capture_remote_bearer_authority,
    release_remote_bearer_authority,
    remote_bearer_authority_for_app,
    require_bearer_token,
)
from omicsclaw.remote.routers.jobs import (
    terminalize_legacy_active_jobs_at_startup,
)
from omicsclaw.remote.runtime_binding import require_remote_workspace
from omicsclaw.version import __version__
from omicsclaw.surfaces.desktop.turn_submission import (
    DEFAULT_MULTIPART_READ_TIMEOUT_SECONDS,
    DesktopMultipartCapacity,
    DesktopMultipartError,
    DesktopAttachmentReferenceV1,
    DesktopTurnAcceptedV1,
    decode_desktop_multipart_submission,
)
from omicsclaw.surfaces.desktop.turn_observation import (
    DesktopTurnCancelResultV1,
    DesktopTurnReceiptV1,
    DesktopTurnSSEBody,
    desktop_turn_receipt_v1,
)
from omicsclaw.surfaces.desktop.run_wire import (
    DESKTOP_RUN_INCIDENT_MAX_PAGE_SIZE,
    DesktopRunAcceptedV1,
    DesktopRunCancelResultV1,
    DesktopRunIntegrityIncidentPageV1,
    DesktopRunReceiptV1,
    DesktopRunSubmissionV1,
    DesktopRunWireError,
    decode_desktop_run_submission,
    desktop_run_integrity_incident_page_v1,
    desktop_run_receipt_from_record,
    desktop_run_receipt_v1,
)
from omicsclaw.surfaces.desktop.wire_contract import (
    desktop_chat_contract,
    desktop_run_contract,
    desktop_turn_observation_contract,
    desktop_turn_submission_contract,
)
from omicsclaw.surfaces.desktop._chat_sse import (
    CHAT_SSE_QUEUE_MAX_ITEMS,
    CHAT_STREAM_COMPARISON_MAX_BYTES,
    render_chat_sse_frame,
    utf8_size,
)
from omicsclaw.skill.resource_scheduler import detect_execution_resource_budget
from omicsclaw.skill.execution.environment import scrub_internal_control_credentials

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("omicsclaw.desktop_server")

DEFAULT_APP_API_HOST = "127.0.0.1"
DEFAULT_APP_API_PORT = 8765
_APP_SERVER_INSTALL_HINT = 'pip install -e ".[desktop]"'
_DEFAULT_APP_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
_OUTPUT_RUNNING_STALE_SECONDS = 30 * 60
_SKILL_EVOLUTION_TOKEN_ENV = "OMICSCLAW_SKILL_EVOLUTION_TOKEN"
_SKILL_EVOLUTION_TOKEN_FD_ENV = "OMICSCLAW_SKILL_EVOLUTION_TOKEN_FD"
_SKILL_EVOLUTION_TOKEN_READ_TIMEOUT_SECONDS = 2.0
_SKILL_EVOLUTION_AUTH_STATE_ATTR = "skill_evolution_bearer_authority"
# Authenticated Backend identity for operation leases.  This value is created
# once per Python server process, is never accepted from the environment, and
# is intentionally omitted from the public remote liveness payload.  A client
# must re-acquire ownership after any Backend restart instead of dispatching an
# old resource id into a replacement process.
_BACKEND_PROCESS_EPOCH = secrets.token_hex(32)
_FILE_TREE_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        ".next",
        "__pycache__",
        ".cache",
        ".turbo",
        "coverage",
        ".output",
        "build",
    }
)

# ---------------------------------------------------------------------------
# Lazy references to omicsclaw.runtime.agent.state — resolved once at startup via lifespan
# ---------------------------------------------------------------------------

_core = None  # omicsclaw.runtime.agent.state module
_memory_client = None  # MemoryClient instance (optional)
_desktop_control_runtime: ControlRuntime | None = None
_desktop_control_runtime_publication_lock = threading.Lock()
_desktop_run_runtime: RunRuntime | None = None
# ADR 0064 background projection sweep (lazily imported in the lifespan).
_projection_service: "MemoryProjectionService | None" = None
_desktop_multipart_capacity = DesktopMultipartCapacity(max_active=2)


@dataclass(frozen=True, slots=True)
class _SkillEvolutionBearerAuthority:
    """Process-lifetime bearer authority for repository-mutating governance."""

    token: str = field(repr=False)
    source: str

    @classmethod
    def capture(
        cls,
        environ: MutableMapping[str, str],
    ) -> "_SkillEvolutionBearerAuthority":
        token_fd = str(environ.pop(_SKILL_EVOLUTION_TOKEN_FD_ENV, "") or "").strip()
        raw_environment_token = str(environ.pop(_SKILL_EVOLUTION_TOKEN_ENV, "") or "")
        environment_token = (
            raw_environment_token if raw_environment_token.strip() else ""
        )
        if token_fd:
            dedicated = _read_skill_evolution_token_fd(token_fd)
            source = "dedicated_pipe"
        elif environment_token:
            if not _is_valid_skill_evolution_dedicated_token(environment_token):
                raise RuntimeError("invalid Skill Evolution credential")
            dedicated = environment_token
            source = "dedicated_environment"
        else:
            dedicated = ""
            source = "unconfigured"
        return cls(token=dedicated, source=source)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def matches(self, candidate: str) -> bool:
        return bool(self.token) and hmac.compare_digest(candidate, self.token)


def _read_skill_evolution_token_fd(raw_fd: str) -> str:
    """Consume one Electron-delivered 256-bit token from an inherited pipe.

    A daemon reader keeps this portable across POSIX and Windows pipes while
    the startup thread enforces a strict deadline.  Timeout is fatal and never
    permits a lower-priority environment credential fallback.
    """

    try:
        fd = int(raw_fd, 10)
    except ValueError as exc:
        raise RuntimeError("invalid Skill Evolution credential descriptor") from exc
    if fd < 3:
        raise RuntimeError("invalid Skill Evolution credential descriptor")

    outcomes: queue.Queue[tuple[bytes | None, Exception | None]] = queue.Queue(
        maxsize=1
    )

    def _consume() -> None:
        payload = bytearray()
        outcome: tuple[bytes | None, Exception | None]
        try:
            while True:
                chunk = os.read(fd, 66 - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > 65:
                    raise RuntimeError("invalid Skill Evolution credential payload")
            outcome = (bytes(payload), None)
        except Exception as exc:
            outcome = (None, exc)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        outcomes.put_nowait(outcome)

    reader = threading.Thread(
        target=_consume,
        name="skill-evolution-credential-reader",
        daemon=True,
    )
    try:
        reader.start()
    except Exception as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        raise RuntimeError("unable to read Skill Evolution credential") from exc

    try:
        raw_payload, read_error = outcomes.get(
            timeout=_SKILL_EVOLUTION_TOKEN_READ_TIMEOUT_SECONDS
        )
    except queue.Empty as exc:
        raise RuntimeError("Skill Evolution credential read timed out") from exc

    if read_error is not None:
        if isinstance(read_error, RuntimeError):
            raise read_error
        raise RuntimeError("unable to read Skill Evolution credential") from read_error
    payload = bytearray(raw_payload or b"")

    if payload.endswith(b"\n"):
        del payload[-1]
    try:
        token = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("invalid Skill Evolution credential payload") from exc
    if not _is_valid_skill_evolution_dedicated_token(token):
        raise RuntimeError("invalid Skill Evolution credential payload")
    return token


def _is_valid_skill_evolution_dedicated_token(token: str) -> bool:
    """Accept only the 256-bit lowercase-hex local authority contract."""

    return len(token) == 64 and all(
        character in "0123456789abcdef" for character in token
    )


def _capture_skill_evolution_bearer_authority(
    app: FastAPI,
    environ: MutableMapping[str, str],
) -> _SkillEvolutionBearerAuthority:
    """Freeze the process-lifetime Skill Evolution authority at startup."""

    authority = _SkillEvolutionBearerAuthority.capture(environ)
    setattr(app.state, _SKILL_EVOLUTION_AUTH_STATE_ATTR, authority)
    if authority.source == "dedicated_environment":
        logger.warning(
            "%s is a manually managed development fallback; packaged Desktop "
            "launches must deliver this credential through the one-shot pipe",
            _SKILL_EVOLUTION_TOKEN_ENV,
        )
    return authority


def _skill_evolution_bearer_policy_for_scope(scope: Scope) -> BearerGatePolicy:
    """Resolve the frozen governance authority for the pre-routing ASGI gate."""

    app = scope.get("app")
    state = getattr(app, "state", None)
    authority = getattr(state, _SKILL_EVOLUTION_AUTH_STATE_ATTR, None)
    token = (
        authority.token if isinstance(authority, _SkillEvolutionBearerAuthority) else ""
    )
    return BearerGatePolicy(
        token=token,
        realm="omicsclaw-skill-evolution",
        unconfigured_detail="skill evolution bearer token is not configured",
    )


def _get_core():
    """Return the omicsclaw.runtime.agent.state module, raising if not initialised."""
    if _core is None:
        raise RuntimeError("OmicsClaw core not initialised. Server startup failed?")
    return _core


def _read_first_config_value(*keys: str) -> str:
    for key in keys:
        value = str(os.environ.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _positive_environment_integer(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return int(raw)


def _app_cors_origins() -> list[str]:
    raw = str(os.getenv("OMICSCLAW_APP_CORS_ORIGINS", "") or "").strip()
    if not raw:
        return list(_DEFAULT_APP_CORS_ORIGINS)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or list(_DEFAULT_APP_CORS_ORIGINS)


def _coerce_kg_home(candidate: str) -> Path:
    """Accept either a project root or a concrete `.omicsclaw/knowledge` path."""
    value = str(candidate or "").strip()
    path = Path(value).expanduser()
    if path.name == "knowledge" and path.parent.name == ".omicsclaw":
        return path
    return path / ".omicsclaw" / "knowledge"


# Bench Phase 3.2 (ADR 0019) — availability of the optional OmicsClaw-KG
# integration, surfaced to the frontend via ``/health`` so it can show/hide
# KG-dependent UI. Set by ``_register_optional_kg_router`` at startup (mirrors
# the ``_NOTEBOOK_AVAILABLE`` flag pattern). Defaults to unavailable.
_KG_AVAILABLE: bool = False
_KG_IMPORT_ERROR: str = ""


def _register_optional_kg_router(app: FastAPI) -> None:
    """Mount OmicsClaw-KG under `/kg` when the optional package is available.

    Also records availability in the module-level ``_KG_AVAILABLE`` /
    ``_KG_IMPORT_ERROR`` flags (Bench Phase 3.2) so ``/health`` can tell the
    frontend whether KG-dependent UI should be shown.
    """
    global _KG_AVAILABLE, _KG_IMPORT_ERROR

    kg_source_dir = str(os.getenv("OMICSCLAW_KG_SOURCE_DIR", "") or "").strip()
    if kg_source_dir and kg_source_dir not in sys.path:
        sys.path.insert(0, kg_source_dir)

    if importlib.util.find_spec("omicsclaw_kg") is None:
        _KG_AVAILABLE = False
        _KG_IMPORT_ERROR = "omicsclaw_kg package not found on the import path"
        logger.info("OmicsClaw-KG package not available; skipping embedded KG routes")
        return

    try:
        from omicsclaw_kg import config as kg_config
        from omicsclaw_kg.http_api import build_router as build_kg_router
        from omicsclaw_kg.http_api import get_kg_config as kg_workspace_dependency
    except ImportError as exc:
        _KG_AVAILABLE = False
        _KG_IMPORT_ERROR = str(exc)
        logger.info("OmicsClaw-KG import failed; skipping embedded KG routes: %s", exc)
        return

    def _embedded_kg_config(
        x_omicsclaw_workspace: str | None = Header(None, alias="X-OmicsClaw-Workspace"),
        workspace: str | None = Query(None),
    ):
        explicit = str(x_omicsclaw_workspace or workspace or "").strip()
        if explicit:
            return kg_config.resolve(_coerce_kg_home(explicit))

        explicit_kg = str(os.getenv("OMICSCLAW_KG_HOME", "") or "").strip()
        if explicit_kg:
            return kg_config.resolve(explicit_kg)

        workspace_root = _resolve_scoped_memory_workspace("")
        if workspace_root:
            return kg_config.resolve(_coerce_kg_home(workspace_root))

        raise HTTPException(
            status_code=400,
            detail=(
                "missing workspace: pass X-OmicsClaw-Workspace, "
                "or configure OMICSCLAW_WORKSPACE / OMICSCLAW_KG_HOME"
            ),
        )

    app.dependency_overrides[kg_workspace_dependency] = _embedded_kg_config
    app.include_router(build_kg_router(enable_writes=True), prefix="/kg")
    _KG_AVAILABLE = True
    _KG_IMPORT_ERROR = ""
    logger.info("Mounted embedded OmicsClaw-KG routes under /kg")


def _resolve_backend_init_config() -> dict[str, str]:
    """Resolve backend startup config from the documented env surface.

    The local setup guide and `.env.example` use the `LLM_*` namespace.
    Older desktop integrations may still populate `OMICSCLAW_*`, so we
    support both and prefer the newer documented keys.
    """
    return {
        "provider": _read_first_config_value("LLM_PROVIDER", "OMICSCLAW_PROVIDER"),
        "api_key": _read_first_config_value("LLM_API_KEY", "OMICSCLAW_API_KEY"),
        "base_url": _read_first_config_value("LLM_BASE_URL", "OMICSCLAW_BASE_URL"),
        "model": _read_first_config_value("OMICSCLAW_MODEL"),
        "auth_mode": (_read_first_config_value("LLM_AUTH_MODE") or "api_key")
        .strip()
        .lower(),
        "ccproxy_port": _read_first_config_value("CCPROXY_PORT") or "11435",
    }


def _current_app_server_port() -> int:
    """Return the effective desktop-server port for this process."""
    raw = str(os.getenv("OMICSCLAW_APP_PORT", "") or "").strip()
    try:
        return int(raw or DEFAULT_APP_API_PORT)
    except (TypeError, ValueError):
        return DEFAULT_APP_API_PORT


def _oauth_port_conflict_message(ccproxy_port: int, app_port: int) -> str:
    return (
        f"ccproxy_port ({ccproxy_port}) conflicts with the OmicsClaw "
        f"desktop-server port ({app_port}). Pick a different port "
        f"(default: 11435) via the CCPROXY_PORT env var or the "
        f"ccproxy_port field in the request body."
    )


# ---------------------------------------------------------------------------
# Provider-aware thinking support
# ---------------------------------------------------------------------------

# Providers whose APIs natively accept the ``thinking`` extra-body parameter.
# For these, ``adaptive`` maps to ``enabled`` with a sensible default budget.
_THINKING_NATIVE_PROVIDERS: frozenset[str] = frozenset(
    {
        "deepseek",
    }
)

# Providers where the ``thinking`` extra-body parameter is known to cause
# gateway errors (e.g. SiliconFlow rejects ``{"type": "adaptive"}``).
# For explicit ``enabled`` requests we still attempt delivery — the user
# made a deliberate choice and should see the provider error if it fails.
_THINKING_INCOMPATIBLE_PROVIDERS: frozenset[str] = frozenset(
    {
        "siliconflow",
    }
)

# Model-name substrings that imply thinking / reasoning support.  Used for
# gateway providers (openrouter, nvidia, volcengine, …) whose capability
# depends on which upstream model is selected rather than the gateway itself.
_THINKING_CAPABLE_MODEL_PATTERNS: tuple[str, ...] = (
    "deepseek-v4",
    "deepseek-r1",
    "deepseek-reasoner",
    "deepseek-chat",
    "deepseek-v3",
)

_DEFAULT_THINKING_BUDGET: int = 10000


def _parse_thinking_budget(thinking: dict) -> int:
    budget = thinking.get("budgetTokens", _DEFAULT_THINKING_BUDGET)
    try:
        return int(budget)
    except (TypeError, ValueError):
        return _DEFAULT_THINKING_BUDGET


def _build_thinking_extra_body(
    thinking: Any,
    *,
    provider: str = "",
    model: str = "",
) -> dict[str, Any] | None:
    """Normalize optional thinking controls for provider-compatible requests.

    The ``adaptive`` thinking type is a frontend UX concept, not a portable
    provider contract.  This function resolves it to a concrete action based
    on the active provider and model:

    * **Native providers** (e.g. ``deepseek``): ``adaptive`` → ``enabled``
      with a default budget so that reasoning tokens are always generated.
    * **Incompatible providers** (e.g. ``siliconflow``): ``adaptive`` → omit
      the field entirely to avoid gateway errors.
    * **Gateway providers** (e.g. ``openrouter``, ``nvidia``): check whether
      the selected *model* name matches a known thinking-capable pattern; if
      so, enable thinking; otherwise omit.
    * **Unknown providers**: omit (safe default).

    Explicit ``enabled`` / ``disabled`` choices from the user are always
    honoured regardless of provider.
    """
    if not isinstance(thinking, dict):
        return None

    thinking_type = str(thinking.get("type", "") or "").strip().lower()

    # -- Explicit user choices: always honour ---------------------------------
    if thinking_type == "enabled":
        return {"type": "enabled", "budget_tokens": _parse_thinking_budget(thinking)}

    if thinking_type == "disabled":
        return {"type": "disabled"}

    # -- Adaptive: resolve per provider/model ---------------------------------
    provider_lower = provider.strip().lower()

    # 1) Known-incompatible gateways → omit
    if provider_lower in _THINKING_INCOMPATIBLE_PROVIDERS:
        return None

    # 2) Native thinking providers → enable
    if provider_lower in _THINKING_NATIVE_PROVIDERS:
        return {"type": "enabled", "budget_tokens": _parse_thinking_budget(thinking)}

    # 3) Gateway / unknown providers → check model name for thinking support
    model_lower = (model or "").strip().lower()
    if model_lower and any(
        pattern in model_lower for pattern in _THINKING_CAPABLE_MODEL_PATTERNS
    ):
        return {"type": "enabled", "budget_tokens": _parse_thinking_budget(thinking)}

    # 4) Unrecognised provider + model → safe default: omit
    return None


# ---------------------------------------------------------------------------
# Active session tracking (for abort support)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ActiveDesktopExecution:
    """One indivisible owner record for a live Desktop chat incarnation."""

    task: asyncio.Task[None]
    cancel_event: threading.Event
    source_request_id: str
    turn_id: str = ""
    source_namespace: str = ""


# Maps session_id -> the exact active execution incarnation.  Keeping task,
# cancellation signal and source identity together prevents a predecessor's
# delayed cleanup from mixing entries across parallel dictionaries.
_active_sessions: dict[str, _ActiveDesktopExecution] = {}

_ABORTED = "aborted"
_ABORT_NOT_FOUND = "not_found"
_ABORT_GENERATION_MISMATCH = "generation_mismatch"
_ABORT_GENERATION_REQUIRED = "generation_required"
_ABORT_TURN_UNRESOLVED = "turn_unresolved"


def _cancel_active_execution(owner: _ActiveDesktopExecution) -> bool:
    """Cancel one lifecycle, returning false when no canonical Turn exists yet."""

    if _desktop_control_runtime is not None:
        if not owner.turn_id and owner.source_namespace and owner.source_request_id:
            owner.turn_id = (
                _desktop_control_runtime.lookup_ingress_turn_id(
                    surface="desktop",
                    source_namespace=owner.source_namespace,
                    source_request_id=owner.source_request_id,
                )
                or ""
            )
        if not owner.turn_id:
            return False
        owner.cancel_event.set()
        _desktop_control_runtime.cancel(owner.turn_id)
        return True

    owner.cancel_event.set()
    owner.task.cancel()
    return True


def _replace_active_execution(
    session_id: str,
    owner: _ActiveDesktopExecution,
) -> None:
    """Install the newest compatibility abort handle without lifecycle effects.

    A second stream for the same Session can be a matching idempotent retry or
    a distinct Turn queued on the same Conversation.  Replacing an observer
    handle must never infer cancellation of either lifecycle.
    """

    previous = _active_sessions.get(session_id)
    if previous is owner:
        return
    if (
        previous is not None
        and previous.turn_id
        and previous.source_request_id == owner.source_request_id
        and previous.source_namespace == owner.source_namespace
    ):
        owner.turn_id = previous.turn_id
    _active_sessions[session_id] = owner


def _release_active_execution(
    session_id: str,
    owner: _ActiveDesktopExecution,
) -> bool:
    """Remove ``owner`` only if it still owns ``session_id``."""

    if _active_sessions.get(session_id) is not owner:
        return False
    _active_sessions.pop(session_id, None)
    return True


_pending_permission_requests: dict[str, dict[str, Any]] = {}
_PERMISSION_APPROVAL_HMAC_KEY = secrets.token_bytes(32)
_session_policy_states: dict[str, ToolPolicyState] = {}
_session_permission_profiles: dict[str, str] = {}
# Bound the per-session policy/profile maps so a long-running desktop backend
# doesn't accumulate one entry per session forever (audit F). Evicting the oldest
# beyond this cap preserves cross-turn approved-tool memory for recent sessions
# (do NOT pop in the run-loop finally — that drops "Allow for this session"
# approvals, which are read back at the next turn's start).
_MAX_TRACKED_SESSIONS = 512

# Cached MCP server entries for prompt injection
_mcp_entries: tuple = ()
_mcp_load_fn = None  # lazy reference to omicsclaw.surfaces.cli._mcp


def _permission_approval_token(
    permission_request_id: str,
    session_id: str,
    tool_name: str,
) -> str:
    """Return a process-local capability bound to one pending approval."""

    message = json.dumps(
        [permission_request_id, session_id, tool_name],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.digest(_PERMISSION_APPROVAL_HMAC_KEY, message, "sha256")
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"v1.{encoded}"


# ---------------------------------------------------------------------------
# Lifespan — initialise OmicsClaw once
# ---------------------------------------------------------------------------


async def _shutdown_desktop_lifespan_state() -> BaseException | None:
    """Attempt every owned cleanup even when one close operation fails."""

    global _channel_manager, _bridge_task
    global _desktop_control_runtime, _desktop_run_runtime, _memory_client
    global _mcp_load_fn, _projection_service
    first_error: BaseException | None = None

    def remember(label: str, exc: BaseException) -> None:
        nonlocal first_error
        if first_error is None:
            first_error = exc
        logger.warning("%s: %s", label, exc)

    channel_manager = _channel_manager
    bridge_task = _bridge_task
    _channel_manager = None
    _bridge_task = None
    if bridge_task and not bridge_task.done():
        try:
            bridge_task.cancel()
            await bridge_task
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            remember("Error stopping bridge task during shutdown", exc)
    if channel_manager is not None:
        try:
            await channel_manager.stop_all()
        except BaseException as exc:
            remember("Error stopping bridge during shutdown", exc)

    # Cancel the projection sweep before its dependencies (repository, Run Store,
    # Memory engine) are closed below.
    projection_service = _projection_service
    _projection_service = None
    if projection_service is not None:
        try:
            await projection_service.close()
        except BaseException as exc:
            remember("Error stopping projection service during shutdown", exc)

    # Every owner keeps its Task and cancellation signal together, preserving
    # the ADR 0009 signal-before-cancel ordering.
    session_tasks: list[asyncio.Task[Any]] = []
    for owner in list(_active_sessions.values()):
        session_tasks.append(owner.task)
        if not _cancel_active_execution(owner):
            owner.cancel_event.set()
            owner.task.cancel()
    _active_sessions.clear()
    if session_tasks:
        outcomes = await asyncio.gather(*session_tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                remember("Active Desktop session shutdown failed", outcome)

    run_runtime = _desktop_run_runtime
    _desktop_run_runtime = None
    if run_runtime is not None:
        # Remote HTTP is only an Adapter over this same Backend-owned Runtime.
        # Remove ingress availability before closing execution ownership.
        from omicsclaw.remote.runtime_binding import unbind_remote_run_runtime

        unbind_remote_run_runtime(run_runtime)
        try:
            run_shutdown = await run_runtime.close()
            unconfirmed = tuple(getattr(run_shutdown, "unconfirmed_run_ids", ()) or ())
            if unconfirmed:
                remember(
                    "RunRuntime shutdown left execution ownership unconfirmed",
                    RuntimeError(
                        f"{len(unconfirmed)} Run execution owner(s) remain unconfirmed"
                    ),
                )
        except BaseException as exc:
            remember("RunRuntime shutdown failed", exc)

    control_runtime = _desktop_control_runtime
    if control_runtime is not None:
        autoagent_stopped = False
        try:
            from omicsclaw.autoagent.api import (
                shutdown_autoagent_repository_binding,
            )

            await shutdown_autoagent_repository_binding(
                control_runtime.repository,
            )
        except BaseException as exc:
            remember("AutoAgent lifecycle shutdown failed", exc)
        else:
            autoagent_stopped = True
        if autoagent_stopped:
            _desktop_control_runtime = None
            try:
                await control_runtime.close()
            except BaseException as exc:
                remember("ControlRuntime shutdown failed", exc)

    if _NOTEBOOK_AVAILABLE:
        try:
            await get_kernel_manager().shutdown_all()
        except BaseException as exc:
            remember("Notebook kernel shutdown failed during app shutdown", exc)

    memory_client = _memory_client
    _memory_client = None
    if memory_client is not None:
        try:
            await memory_client.close()
        except BaseException as exc:
            remember("MemoryClient shutdown failed", exc)
    _mcp_load_fn = None
    return first_error


@asynccontextmanager
async def _desktop_runtime_lifespan(app: FastAPI):
    global _core, _memory_client, _desktop_control_runtime, _desktop_run_runtime

    # Resolve OmicsClaw project dir — either from env or auto-detect
    omicsclaw_dir = os.getenv("OMICSCLAW_DIR", "")
    if omicsclaw_dir:
        omicsclaw_dir = str(Path(omicsclaw_dir).resolve())
        if omicsclaw_dir not in sys.path:
            sys.path.insert(0, omicsclaw_dir)

    try:
        import omicsclaw.runtime.agent.state as core

        _core = core
    except ImportError as exc:
        logger.error(
            "Cannot import omicsclaw.runtime.agent.state — is OmicsClaw on sys.path or "
            "OMICSCLAW_DIR set? Error: %s",
            exc,
        )
        raise

    # Initialise the LLM client from environment / provider config
    startup_config = _resolve_backend_init_config()
    provider = startup_config["provider"]
    api_key = startup_config["api_key"]
    base_url = startup_config["base_url"]
    model = startup_config["model"]
    auth_mode = startup_config.get("auth_mode", "api_key")
    try:
        ccproxy_port = int(startup_config.get("ccproxy_port", "11435") or "11435")
    except (TypeError, ValueError):
        ccproxy_port = 11435

    if str(auth_mode or "").strip().lower() == "oauth":
        app_port = _current_app_server_port()
        if ccproxy_port == app_port:
            logger.warning(
                "Falling back to auth_mode='api_key' at startup — %s",
                _oauth_port_conflict_message(ccproxy_port, app_port),
            )
            auth_mode = "api_key"

    # Bootstrap (not user-initiated): if OAuth setup fails — e.g. stale
    # LLM_AUTH_MODE=oauth in .env but ccproxy uninstalled — log a warning
    # and fall back to api_key mode instead of crashing the entire server.
    # The user can still reach the UI and fix the config.
    core.init(
        api_key=api_key,
        base_url=base_url or None,
        model=model,
        provider=provider,
        auth_mode=auth_mode,
        ccproxy_port=ccproxy_port,
        strict_oauth=False,
        allow_missing_credentials=True,
    )
    logger.info(
        "OmicsClaw core initialised: provider=%s model=%s",
        core.LLM_PROVIDER_NAME,
        core.OMICSCLAW_MODEL,
    )

    desktop_workspace_path = (
        Path(str(os.getenv("OMICSCLAW_WORKSPACE", "") or Path.cwd()))
        .expanduser()
        .resolve()
    )
    if not desktop_workspace_path.is_dir():
        raise RuntimeError(
            f"desktop workspace directory does not exist: {desktop_workspace_path}"
        )
    desktop_workspace = str(desktop_workspace_path)
    control_runtime = ControlRuntime.for_local_surface(
        workspace_id=desktop_workspace,
        surface="desktop",
        installation_id="local",
        profile_id="owner",
        attachment_input_enabled=True,
    )
    try:
        await control_runtime.start()
    except BaseException:
        try:
            await control_runtime.close()
        except BaseException as close_error:
            logger.warning(
                "ControlRuntime cleanup after start failure failed (%s)",
                type(close_error).__name__,
            )
        raise
    with _desktop_control_runtime_publication_lock:
        published = _desktop_control_runtime is None
        if published:
            _desktop_control_runtime = control_runtime
    if not published:
        try:
            await control_runtime.close()
        except BaseException as close_error:
            logger.warning(
                "Unpublished ControlRuntime cleanup failed (%s)",
                type(close_error).__name__,
            )
        raise RuntimeError("Desktop ControlRuntime is already started")
    try:
        from omicsclaw.autoagent.api import bind_governed_autoagent_repository

        reconciled_autoagent = await bind_governed_autoagent_repository(
            control_runtime.repository
        )
        # Legacy closure writes are allowed only after this Backend owns the
        # lifetime Control lock.  A competing process must fail before either
        # instance mutates compatibility state.
        legacy_interrupted_job_ids = terminalize_legacy_active_jobs_at_startup(
            desktop_workspace_path
        )
    except BaseException:
        await _shutdown_desktop_lifespan_state()
        raise
    logger.info(
        "Desktop ControlRuntime initialised: state_root=%s",
        control_runtime.repository.state_root,
    )
    if reconciled_autoagent.interrupted_session_ids:
        logger.warning(
            "Interrupted %d inherited AutoAgent session(s) without replay",
            len(reconciled_autoagent.interrupted_session_ids),
        )
    if reconciled_autoagent.unconfirmed_session_ids:
        logger.warning(
            "Quarantined %d inherited AutoAgent owner(s); novel starts are disabled",
            len(reconciled_autoagent.unconfirmed_session_ids),
        )
    if legacy_interrupted_job_ids:
        logger.warning(
            "Closed %d unrecoverable legacy scientific Job(s) without replay",
            len(legacy_interrupted_job_ids),
        )
    run_output_root = Path(
        str(os.getenv("OMICSCLAW_OUTPUT_ROOT", "") or "").strip()
        or str(Path(desktop_workspace) / "output")
    )
    try:
        _desktop_run_runtime = RunRuntime.for_local_surface(
            repository=control_runtime.repository,
            output_root=run_output_root,
            resource_budget=detect_execution_resource_budget(run_output_root),
            max_buffered_runs=_positive_environment_integer(
                "OMICSCLAW_RUN_BUFFER_CAPACITY", 32
            ),
            max_active_runs=_positive_environment_integer(
                "OMICSCLAW_RUN_MAX_ACTIVE", 2
            ),
        )
        reconciled_runs = await _desktop_run_runtime.start()
        from omicsclaw.remote.runtime_binding import bind_remote_run_runtime

        bind_remote_run_runtime(
            _desktop_run_runtime,
            workspace=desktop_workspace,
        )
    except BaseException:
        await _shutdown_desktop_lifespan_state()
        raise
    logger.info(
        "Desktop RunRuntime initialised: output_root=%s interrupted_runs=%d "
        "unconfirmed_runs=%d",
        run_output_root,
        len(reconciled_runs.interrupted_run_ids),
        len(reconciled_runs.unconfirmed_run_ids),
    )
    if reconciled_runs.unconfirmed_run_ids:
        logger.error(
            "Run admission quarantined: %d inherited execution owner(s) or "
            "completion evidence could not be confirmed",
            len(reconciled_runs.unconfirmed_run_ids),
        )
    if _NOTEBOOK_AVAILABLE:
        try:
            install_live_session_support()
        except Exception as exc:
            logger.warning("Notebook live-session support unavailable: %s", exc)
    else:
        logger.info(
            "Notebook module not available (non-fatal): %s",
            _NOTEBOOK_IMPORT_ERROR or "(import failed without message)",
        )

    # Optionally expose MemoryClient for browse/search endpoints. The
    # desktop client is bound to a launch-id-derived namespace
    # (``app/<launch_id>`` or ``app/desktop_user`` for single-user runs)
    # so its writes/reads are partitioned from the bot's per-user
    # namespaces and from CLI workspace namespaces.
    try:
        from omicsclaw.memory import (
            desktop_namespace,
            get_engine_db,
            get_memory_client,
        )

        await get_engine_db().init_db()

        from omicsclaw.memory import get_memory_engine
        from omicsclaw.memory.bootstrap import seed_knowhows

        await seed_knowhows(get_memory_engine())
        _memory_client = get_memory_client(namespace=desktop_namespace())
        logger.info("MemoryClient initialised (namespace=%s)", _memory_client.namespace)
    except asyncio.CancelledError:
        await _shutdown_desktop_lifespan_state()
        raise
    except Exception as exc:
        logger.warning("MemoryClient unavailable (non-fatal): %s", exc)
        _memory_client = None

    # ADR 0064: start the background sweep that applies pending Project
    # Projection Intents (frozen at Run terminalization) into Project-scoped
    # Memory. Best-effort and non-fatal; requires the control repository, the Run
    # Store, and a live Memory engine — all of which coexist only in this Desktop
    # backend. A None _memory_client means the engine is down, so skip the sweep.
    global _projection_service
    try:
        if (
            _memory_client is not None
            and _desktop_run_runtime is not None
            and _desktop_control_runtime is not None
        ):
            from omicsclaw.memory import get_memory_client as _get_memory_client
            from omicsclaw.memory.projection_service import MemoryProjectionService
            from omicsclaw.memory.projection_source import RunManifestSourceReader

            _projection_service = MemoryProjectionService(
                repository=_desktop_control_runtime.repository,
                read_source=RunManifestSourceReader(
                    _desktop_run_runtime.run_store.read_manifest
                ),
                client_factory=lambda ns: _get_memory_client(namespace=ns),
            )
            _projection_service.start()
            logger.info("Memory projection service started")
    except Exception as exc:
        logger.warning("Memory projection service unavailable (non-fatal): %s", exc)
        _projection_service = None

    # Optionally load MCP server support
    global _mcp_load_fn
    try:
        from omicsclaw.surfaces.cli._mcp import (
            load_active_mcp_server_entries_for_prompt,
            list_mcp_servers,
        )

        _mcp_load_fn = load_active_mcp_server_entries_for_prompt
        logger.info(
            "MCP support loaded (%d servers configured)", len(list_mcp_servers())
        )
    except Exception as exc:
        logger.info("MCP module not available (non-fatal): %s", exc)
        _mcp_load_fn = None

    try:
        yield
    finally:
        original_error = sys.exc_info()[1]
        cleanup_error = await _shutdown_desktop_lifespan_state()
        if original_error is None and cleanup_error is not None:
            raise cleanup_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own immutable HTTP authorities for exactly one Backend lifespan."""

    # Runtime environment mutation cannot rotate or disable either authority;
    # selecting new credentials requires a deliberate Backend restart.
    remote_bearer_authority = capture_remote_bearer_authority(app, os.environ)
    skill_evolution_authority: _SkillEvolutionBearerAuthority | None = None
    try:
        # Capture the narrow repository-mutation capability before any Runtime
        # or Skill subprocess can inherit it. The remote token remains in the
        # separately operated Backend environment; execution children scrub it.
        skill_evolution_authority = _capture_skill_evolution_bearer_authority(
            app, os.environ
        )
        async with _desktop_runtime_lifespan(app):
            yield
    finally:
        release_remote_bearer_authority(app, remote_bearer_authority)
        if (
            skill_evolution_authority is not None
            and getattr(app.state, _SKILL_EVOLUTION_AUTH_STATE_ATTR, None)
            is skill_evolution_authority
        ):
            delattr(app.state, _SKILL_EVOLUTION_AUTH_STATE_ATTR)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OmicsClaw-App Backend",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(AutoAgentStartBodyGuardMiddleware)
app.add_middleware(
    RemoteBearerMiddleware,
    public_paths=("/health",),
    delegated_path_prefixes=("/skill-evolution",),
    delegated_policy_resolver=_skill_evolution_bearer_policy_for_scope,
    backend_process_epoch_resolver=lambda _scope: _BACKEND_PROCESS_EPOCH,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_app_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount autoagent optimization router
try:
    from omicsclaw.autoagent.api import router as optimize_router

    app.include_router(optimize_router)
except ImportError:
    pass  # autoagent module not available

_register_optional_kg_router(app)

if _NOTEBOOK_AVAILABLE:
    app.include_router(notebook_router, prefix="/notebook", tags=["notebook"])

# Remote control-plane API consumed by OmicsClaw-App (see omicsclaw/remote/).
from omicsclaw.remote.app_integration import register_remote_routers  # noqa: E402

register_remote_routers(app)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ProviderConfig(BaseModel):
    """Optional per-request provider override."""

    provider: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""


class ChatRequest(BaseModel):
    """POST /chat/stream body."""

    # Desktop ingress identity.  The production lifespan owns a ControlRuntime;
    # direct dispatch remains only as a test Adapter for handler-level tests.
    ingress_schema_version: StrictInt = Field(default=1, ge=1, le=1)
    source_request_id: str = Field(
        default="", max_length=32, pattern=r"^(?:|[0-9a-f]{32})$"
    )
    installation_id: str = Field(
        default="", max_length=32, pattern=r"^(?:|[0-9a-f]{32})$"
    )
    profile_id: str = Field(default="", max_length=32, pattern=r"^(?:|owner)$")
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = ""
    content: str
    workspace: str = ""
    pipeline_workspace: str = ""
    output_style: str = ""
    provider_config: Optional[ProviderConfig] = None
    # Runtime controls forwarded from desktop-app frontend
    model: str = ""
    mode: str = ""
    provider_id: str = ""
    effort: str = ""
    thinking: Optional[dict] = None
    context_1m: bool = False
    permission_profile: str = "default"
    files: Optional[list[dict]] = None
    file_selections: Optional[list[dict]] = None
    attachment_descriptors: Optional[list[dict]] = None
    system_prompt_append: str = ""
    # Bench — study-scoped investigation thread (ADR 0018) + lifecycle stage
    # lens (ADR 0020). Phase 0: both fields are accepted but inert (no thread
    # binding, no stage tool gating yet); consumers arrive in Phase 1A / Phase 2.
    # Invalid stage strings are tolerated (treated as default), never rejected.
    thread_id: str = ""
    stage: str = ""


def _desktop_ingress_identity(req: ChatRequest) -> tuple[str, str, str]:
    """Derive the local Desktop identity without accepting an Owner partition."""

    installation_id = req.installation_id or "local"
    profile_id = "owner"
    source_namespace = f"desktop/v1/{installation_id}/{profile_id}"
    return installation_id, profile_id, source_namespace


class AbortRequest(BaseModel):
    """POST /chat/abort body."""

    session_id: str
    source_request_id: str = Field(
        default="", max_length=32, pattern=r"^(?:|[0-9a-f]{32})$"
    )


class PermissionResponseRequest(BaseModel):
    """POST /chat/permission body."""

    permissionRequestId: str
    approvalToken: str = Field(
        min_length=46,
        max_length=46,
        pattern=r"^v1\.[A-Za-z0-9_-]{43}$",
    )
    decision: dict[str, Any]


class SessionPermissionProfileRequest(BaseModel):
    """POST /chat/session-permission-profile body."""

    session_id: str
    permission_profile: str = "default"


class SkillInstallRequest(BaseModel):
    """POST /skills/install body."""

    source: str


class SkillUninstallRequest(BaseModel):
    """POST /skills/uninstall body."""

    name: str


EvolutionIdentity = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
EvolutionReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
EvolutionEventId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
EvolutionGotchaLead = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=160,
        pattern=r"^[^\r\n`*<>\[\]!#\\{}|~]+$",
    ),
]
EvolutionGotchaDetail = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=500,
        pattern=r"^[^\r\n`*<>\[\]!#\\{}|~]+$",
    ),
]
EvolutionGotchaAnchor = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+\.py:[1-9][0-9]*$",
    ),
]


class _EvolutionRequest(BaseModel):
    """Strict schema shared only by Skill evolution HTTP contracts."""

    model_config = {"extra": "forbid"}


class EvolutionApprovalRequest(_EvolutionRequest):
    """Human decision for one evidence-bound evolution proposal."""

    approver: EvolutionIdentity
    reason: EvolutionReason


class EvolutionRejectionRequest(_EvolutionRequest):
    """Audited rejection for one evolution proposal."""

    approver: EvolutionIdentity
    reason: EvolutionReason


class EvolutionReconciliationRequest(_EvolutionRequest):
    """Operator audit fields for interrupted approval recovery."""

    operator: EvolutionIdentity
    reason: EvolutionReason


class EvolutionDeprecationProposalRequest(_EvolutionRequest):
    """Backend-owned evidence and replacement identity for a lifecycle candidate."""

    target_skill: EvolutionIdentity
    replacement_skill: EvolutionIdentity
    proposer: EvolutionIdentity
    reason: EvolutionReason
    support_event_ids: list[EvolutionEventId] = Field(min_length=1, max_length=100)


class EvolutionGotchaEntryRequest(_EvolutionRequest):
    """Narrative fields rendered by Backend into one canonical Gotcha bullet."""

    lead: EvolutionGotchaLead
    condition: EvolutionGotchaDetail
    guidance: EvolutionGotchaDetail
    anchors: list[EvolutionGotchaAnchor] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_governed_narrative(self):
        # Reuse the Backend governance policy so HTTP validation cannot drift
        # from the eventual proposal/materialization gate.  Raising here keeps
        # malformed or privacy-sensitive narrative at a 422 boundary.
        from omicsclaw.skill.evolution_governance import SkillEvolutionGovernance

        SkillEvolutionGovernance._normalize_gotcha_entry(self.model_dump())
        return self


class EvolutionGotchaProposalRequest(_EvolutionRequest):
    """Evidence identifiers plus structured narrative; never a path or patch."""

    target_skill: EvolutionIdentity
    proposer: EvolutionIdentity
    reason: EvolutionReason
    support_event_ids: list[EvolutionEventId] = Field(min_length=1, max_length=100)
    entry: EvolutionGotchaEntryRequest


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_line(event_type: str, data: Any) -> str:
    """Format one frame under the Desktop Chat 4 MiB wire contract."""

    return render_chat_sse_frame(event_type, data)


def _sse_error(message: str) -> str:
    return _sse_line("error", message)


def _sse_done() -> str:
    return _sse_line("done", "")


def _omicsclaw_project_dir() -> Path:
    core = _get_core()
    omicsclaw_dir = getattr(core, "OMICSCLAW_DIR", "")
    if omicsclaw_dir:
        return Path(omicsclaw_dir).resolve()

    env_value = os.getenv("OMICSCLAW_DIR", "")
    if env_value:
        return Path(env_value).resolve()

    raise RuntimeError("OMICSCLAW_DIR is not available")


def _serialize_skill_command_statuses(statuses: list[Any]) -> list[dict[str, str]]:
    return [
        {
            "level": str(getattr(status, "level", "info") or "info"),
            "text": str(getattr(status, "text", "") or ""),
        }
        for status in statuses
    ]


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _runtime_health_payload(core: Any) -> dict[str, Any]:
    skill_python = (
        core.get_skill_runner_python()
        if hasattr(core, "get_skill_runner_python")
        else str(getattr(core, "PYTHON", "") or "").strip() or sys.executable
    )
    omicsclaw_dir = ""
    try:
        omicsclaw_dir = str(_omicsclaw_project_dir())
    except Exception:
        omicsclaw_dir = ""

    return {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "skill_python_executable": skill_python,
        "omicsclaw_dir": omicsclaw_dir,
        "launch_id": str(os.getenv("OMICSCLAW_DESKTOP_LAUNCH_ID", "") or ""),
        "dependencies": {
            "cellcharter": _module_available("cellcharter"),
            "squidpy": _module_available("squidpy"),
        },
    }


async def _resolve_and_bind_thread_id(
    session_manager: Any, user_id: str, session_id: str, req_thread_id: str
) -> str:
    """Resolve a turn's thread_id and bind it to the session (ADR 0023 decision 3).

    Returns ``request.thread_id`` if set; otherwise the bound session's stored
    ``thread_id`` (so a turn that omits the field still rolls up to its thread).
    When a thread_id is resolved, ``get_or_create`` stamps a *freshly-created*
    session with it (an existing session's binding is immutable — rebinding is
    v1.5). The session id mirrors ``SessionManager.get_or_create``'s
    ``f"{platform}:{user_id}:{chat_id}"`` form. Best-effort: any failure returns
    the request's value and never blocks the turn.
    """
    resolved = req_thread_id
    if session_manager is None:
        return resolved
    try:
        full_session_id = f"app:{user_id}:{session_id}"
        if not resolved:
            # SessionManager wraps the store; get_session lives on .store.
            store = getattr(session_manager, "store", None)
            existing = await store.get_session(full_session_id) if store else None
            if existing is not None and getattr(existing, "thread_id", ""):
                resolved = existing.thread_id
        if resolved:
            await session_manager.get_or_create(
                user_id, "app", session_id, thread_id=resolved
            )
    except Exception as exc:  # pragma: no cover - never block a turn on this
        logger.warning("thread_id session resolution failed: %s", exc)
    return resolved


def _resolve_shared_kg_home() -> str:
    """The shared KG home the desktop ``/kg`` routes resolve to, without a request
    header (mirrors the env precedence in ``_embedded_kg_config``:
    ``OMICSCLAW_KG_HOME`` else ``OMICSCLAW_WORKSPACE`` / ``DATA_DIR`` coerced to
    ``<ws>/.omicsclaw/knowledge``). Empty when none set.

    The result is ``.resolve()``-normalized to match what the routes actually
    serve: ``KGConfig.__post_init__`` resolves the home, so a non-canonical or
    relative input would otherwise be reported here differently than served.
    """
    explicit = str(os.getenv("OMICSCLAW_KG_HOME", "") or "").strip()
    if explicit:
        return str(Path(explicit).resolve())
    workspace_root = _resolve_scoped_memory_workspace("")
    if workspace_root:
        return str(_coerce_kg_home(workspace_root).resolve())
    return ""


def _skills_count_payload(core: Any) -> dict[str, Any]:
    """Primary skill count for /health. ``registry.load_all`` raises
    (ADR 0042-0072 tightened a missing/invalid skills root from a silent
    no-op to a hard error, and that's correct — see test_registry_discovery.py)
    when the skills root hasn't been provisioned yet (e.g. first launch of the
    bundled desktop runtime before ``OMICSCLAW_DIR/skills`` exists). That's a
    real, expected state for /health to report, not a reason to 500 the whole
    endpoint over one field — degrade to null + the error string instead,
    mirroring _kg_status_payload's home-resolution fallback below.
    """
    try:
        return {"skills_count": core._primary_skill_count()}
    except Exception as exc:  # pragma: no cover - never break /health on skill-registry load failure
        return {"skills_count": None, "skills_count_error": str(exc)}


def _kg_status_payload() -> dict[str, Any]:
    """Availability of the optional OmicsClaw-KG integration, for the frontend
    (Bench Phase 3.2, ADR 0019). ``available`` is True iff the embedded ``/kg``
    routes mounted at startup — the frontend shows/hides KG-dependent UI on this
    flag. ``home`` is the shared KG home those routes resolve to (best-effort,
    empty if unavailable); ``import_error`` is included only when unavailable.
    """
    home = ""
    if _KG_AVAILABLE:
        try:
            home = _resolve_shared_kg_home()
        except Exception:  # pragma: no cover - never break /health on home resolution
            home = ""
    payload: dict[str, Any] = {"available": _KG_AVAILABLE, "home": home}
    if not _KG_AVAILABLE and _KG_IMPORT_ERROR:
        payload["import_error"] = _KG_IMPORT_ERROR
    return payload


def _resolve_scoped_memory_workspace(explicit_workspace: str = "") -> str:
    workspace = str(explicit_workspace or "").strip()
    if workspace:
        return workspace

    workspace = str(os.getenv("OMICSCLAW_WORKSPACE", "") or "").strip()
    if workspace:
        return workspace

    try:
        core = _get_core()
        return str(getattr(core, "DATA_DIR", "") or "").strip()
    except Exception:
        return ""


def _permission_profile_to_policy_state(
    session_id: str,
    permission_profile: str,
) -> ToolPolicyState:
    persisted = _session_policy_states.get(session_id, ToolPolicyState(surface="app"))
    profile = str(permission_profile or "default").strip().lower()
    if profile == "full_access":
        return ToolPolicyState(
            surface="app",
            trusted=True,
            auto_approve_ask=True,
            approved_tool_names=persisted.approved_tool_names,
        )
    return ToolPolicyState(
        surface="app",
        approved_tool_names=persisted.approved_tool_names,
    )


def _normalize_permission_profile(permission_profile: str) -> str:
    profile = str(permission_profile or "default").strip().lower()
    return "full_access" if profile == "full_access" else "default"


def _effective_permission_profile(session_id: str, fallback: str = "default") -> str:
    return _session_permission_profiles.get(
        session_id,
        _normalize_permission_profile(fallback),
    )


def _set_session_permission_profile(
    session_id: str,
    permission_profile: str,
) -> ToolPolicyState:
    normalized = _normalize_permission_profile(permission_profile)
    # Compute next_state BEFORE re-inserting — _permission_profile_to_policy_state
    # reads the persisted approved_tool_names from _session_policy_states (the
    # cross-turn "Allow for this session" carry); popping first would lose it.
    next_state = _permission_profile_to_policy_state(session_id, normalized)
    # pop+reinsert moves this session to the most-recent (end) position so the
    # insertion-order eviction in _evict_stale_session_state is LRU-by-turn-start
    # (a reused old session isn't dropped before idle newer ones).
    _session_permission_profiles.pop(session_id, None)
    _session_permission_profiles[session_id] = normalized
    _session_policy_states.pop(session_id, None)
    _session_policy_states[session_id] = next_state
    _evict_stale_session_state()
    return next_state


def _evict_stale_session_state() -> None:
    """Bound the per-session policy/profile maps (audit F). dict preserves
    insertion order, so dropping ``next(iter(...))`` evicts the oldest beyond the
    cap. Cross-turn approved-tool memory is preserved for the most recent sessions;
    an evicted session simply re-establishes default state on next use."""
    for store in (_session_policy_states, _session_permission_profiles):
        while len(store) > _MAX_TRACKED_SESSIONS:
            store.pop(next(iter(store)), None)


def _with_approved_tools(
    state: ToolPolicyState,
    tool_names: set[str],
) -> ToolPolicyState:
    if not tool_names:
        return state
    return ToolPolicyState(
        surface=state.surface,
        trusted=state.trusted,
        background=state.background,
        auto_approve_ask=state.auto_approve_ask,
        approved_tool_names=state.approved_tool_names | frozenset(tool_names),
    )


def _tool_names_from_permission_suggestions(
    suggestions: list[dict[str, Any]] | None,
) -> set[str]:
    tool_names: set[str] = set()
    for suggestion in suggestions or []:
        if not isinstance(suggestion, dict):
            continue
        for rule in suggestion.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            tool_name = str(rule.get("toolName", "") or "").strip()
            if tool_name:
                tool_names.add(tool_name)
    return tool_names


def _build_token_usage(
    response_usage: Any,
    usage_totals: dict[str, float],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    prompt_tokens = int(getattr(response_usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(response_usage, "completion_tokens", 0) or 0)

    # Extract cache tokens from all known provider formats:
    #   OpenAI:    prompt_tokens_details.cached_tokens / .cache_creation_tokens
    #   DeepSeek:  prompt_cache_hit_tokens (top-level)
    #   Anthropic: cache_read_input_tokens / cache_creation_input_tokens (top-level)
    prompt_details = getattr(response_usage, "prompt_tokens_details", None)
    cache_read_tokens = int(getattr(prompt_details, "cached_tokens", 0) or 0)
    cache_creation_tokens = int(
        getattr(prompt_details, "cache_creation_tokens", 0) or 0
    )
    # DeepSeek: prompt_cache_hit_tokens
    if not cache_read_tokens:
        cache_read_tokens = int(
            getattr(response_usage, "prompt_cache_hit_tokens", 0) or 0
        )
    # Anthropic: cache_read_input_tokens / cache_creation_input_tokens
    if not cache_read_tokens:
        cache_read_tokens = int(
            getattr(response_usage, "cache_read_input_tokens", 0) or 0
        )
    if not cache_creation_tokens:
        cache_creation_tokens = int(
            getattr(response_usage, "cache_creation_input_tokens", 0) or 0
        )

    usage_totals["input_tokens"] += prompt_tokens
    usage_totals["output_tokens"] += completion_tokens
    usage_totals["cache_read_input_tokens"] += cache_read_tokens
    usage_totals["cache_creation_input_tokens"] += cache_creation_tokens

    core = _get_core()
    cost_usd = 0.0
    get_prices = getattr(core, "_get_token_price", None)
    if callable(get_prices):
        input_price, output_price = get_prices(model or core.OMICSCLAW_MODEL)
        # F: bill cache-READ input at a discount (consistent with the
        # cache_hit_ratio / fresh_input_tokens surfaced below) instead of charging
        # every input token at full price — a cache-heavy session is otherwise
        # over-reported ~5–10x. fresh = gross input − cache reads.
        disc_fn = getattr(core, "_cache_read_discount", None)
        discount = disc_fn() if callable(disc_fn) else 0.1
        cache_read = usage_totals["cache_read_input_tokens"]
        fresh_input = max(0, usage_totals["input_tokens"] - cache_read)
        cost_usd = (
            fresh_input / 1_000_000 * float(input_price or 0.0)
            + cache_read / 1_000_000 * float(input_price or 0.0) * discount
            + usage_totals["output_tokens"] / 1_000_000 * float(output_price or 0.0)
        )

    usage_payload: dict[str, Any] = {
        "input_tokens": int(usage_totals["input_tokens"]),
        "output_tokens": int(usage_totals["output_tokens"]),
    }
    if usage_totals["cache_read_input_tokens"] > 0:
        usage_payload["cache_read_input_tokens"] = int(
            usage_totals["cache_read_input_tokens"]
        )
    if usage_totals["cache_creation_input_tokens"] > 0:
        usage_payload["cache_creation_input_tokens"] = int(
            usage_totals["cache_creation_input_tokens"]
        )
    # ADR 0024 §5 — surface the cache economics so the gross input_tokens total is
    # interpretable. With a byte-stable prefix most re-sent input is cache hits
    # (~10% the price of a miss on DeepSeek), so fresh_input_tokens — the tokens
    # actually re-processed — is far below the gross input_tokens the UI shows.
    total_input = usage_totals["input_tokens"]
    cached_input = usage_totals["cache_read_input_tokens"]
    if total_input > 0 and cached_input > 0:
        usage_payload["cache_hit_ratio"] = round(cached_input / total_input, 4)
        usage_payload["fresh_input_tokens"] = int(max(0.0, total_input - cached_input))
    if cost_usd > 0:
        usage_payload["cost_usd"] = round(cost_usd, 6)
    return usage_payload


def _extract_blocked_path(arguments: dict[str, Any]) -> str:
    for key in (
        "path",
        "file_path",
        "destination",
        "source",
        "workspace",
        "pipeline_workspace",
    ):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# POST /chat/stream — SSE streaming chat
# ---------------------------------------------------------------------------


async def _handle_slash_command(command: str, arg: str, session_id: str) -> str | None:
    """
    Handle slash commands locally without going through the LLM.
    Returns the text response, or None if the command is not handled
    (falls through to ``dispatch``).
    """
    core = _get_core()

    if command == "/skills":
        registry = core._skill_registry()
        lines = []
        domain_filter = arg.strip().lower() if arg.strip() else None

        # Group by domain
        domain_skills: dict[str, list[str]] = {}
        for alias, info in registry.skills.items():
            if alias != info.get("alias", alias):
                continue
            domain = info.get("domain", "other")
            if domain_filter and domain_filter not in domain.lower():
                continue
            domain_skills.setdefault(domain, []).append(
                f"  - {alias}: {info.get('description', '')[:80]}"
            )

        if not domain_skills:
            return f"No skills found{' for domain: ' + arg.strip() if arg.strip() else ''}."

        for domain in sorted(domain_skills.keys()):
            lines.append(f"\n**{domain}** ({len(domain_skills[domain])} skills)")
            lines.extend(sorted(domain_skills[domain]))

        total = sum(len(v) for v in domain_skills.values())
        header = f"## OmicsClaw Skills ({total} total)"
        if domain_filter:
            header += f" — filtered: {arg.strip()}"
        return header + "\n" + "\n".join(lines)

    elif command == "/usage":
        usage = core.get_usage_snapshot()
        lines = ["## Usage Statistics"]
        for k, v in usage.items():
            lines.append(f"- **{k}**: {v}")
        return "\n".join(lines)

    elif command == "/context":
        return (
            f"## Context Info\n"
            f"- Model: {core.OMICSCLAW_MODEL}\n"
            f"- Provider: {core.LLM_PROVIDER_NAME}\n"
            f"- Max history: {core.MAX_HISTORY}\n"
            f"- Max tool iterations: {core.MAX_TOOL_ITERATIONS}\n"
            f"- Data dir: {core.DATA_DIR}\n"
            f"- Output dir: {core.OUTPUT_DIR}"
        )

    elif command == "/doctor":
        import platform as plat

        skills_status = _skills_count_payload(core)
        skills_line = (
            f"- Skills: {skills_status['skills_count']}"
            if skills_status.get("skills_count") is not None
            else f"- Skills: unavailable ({skills_status.get('skills_count_error', 'unknown error')})"
        )
        lines = [
            "## Environment Diagnostics",
            f"- Python: {plat.python_version()}",
            f"- Platform: {plat.platform()}",
            f"- Provider: {core.LLM_PROVIDER_NAME}",
            f"- Model: {core.OMICSCLAW_MODEL}",
            skills_line,
            f"- Tools: {len(core.get_tool_executors())}",
            f"- Memory: {'enabled' if _memory_client else 'disabled'}",
            f"- MCP: {'loaded' if _mcp_load_fn else 'not available'}",
        ]
        return "\n".join(lines)

    elif command == "/help":
        try:
            from omicsclaw.surfaces.cli._constants import SLASH_COMMANDS

            lines = ["## Available Commands\n"]
            for cmd, desc in SLASH_COMMANDS:
                lines.append(f"- `{cmd}` — {desc}")
            return "\n".join(lines)
        except ImportError:
            return "Help not available (interactive module not found)."

    elif command == "/mcp":
        if _mcp_load_fn is None:
            return "MCP module not available."
        try:
            from omicsclaw.surfaces.cli._mcp import list_mcp_servers

            servers = list_mcp_servers()
            if not servers:
                return "No MCP servers configured."
            lines = ["## MCP Servers\n"]
            for s in servers:
                status = "active" if s.get("active") else "configured"
                lines.append(
                    f"- **{s['name']}** ({s.get('transport', 'stdio')}) — {status}"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"MCP error: {exc}"

    elif command == "/style":
        if arg.strip():
            return f"Style switching via desktop app is not yet supported. Use the CLI: `omicsclaw` → `/style set {arg.strip()}`"
        return "Use `/style list` or `/style set <name>` in the OmicsClaw CLI."

    elif command == "/config":
        settings = {
            "provider": core.LLM_PROVIDER_NAME,
            "model": core.OMICSCLAW_MODEL,
            "max_history": core.MAX_HISTORY,
            "data_dir": str(core.DATA_DIR),
            "output_dir": str(core.OUTPUT_DIR),
        }
        lines = ["## Configuration\n"]
        for k, v in settings.items():
            lines.append(f"- **{k}**: `{v}`")
        return "\n".join(lines)

    # Not handled locally → pass through to LLM
    return None


@app.post("/chat/stream", dependencies=[Depends(require_bearer_token)])
async def chat_stream(req: ChatRequest):
    """
    Run dispatch(envelope) and stream events via SSE (per ADR 0006).

    Events emitted:
      - {"type": "status",             "data": "{...}"} — init/status metadata
      - {"type": "mode_changed",       "data": "..."}   — current SDK mode
      - {"type": "text",               "data": "..."}   — streamed LLM text chunk
      - {"type": "tool_use",           "data": "{...}"} — tool call start
      - {"type": "tool_output",        "data": "..."}   — tool progress/output
      - {"type": "tool_log",           "data": "{...}"} — live skill stdout/stderr log lines
      - {"type": "tool_result",        "data": "{...}"} — tool result
      - {"type": "tool_timeout",       "data": "{...}"} — tool timed out
      - {"type": "permission_request", "data": "{...}"} — approval required
      - {"type": "ask_user_question",  "data": "{...}"} — interactive choice prompt
      - {"type": "task_update",        "data": "{...}"} — task/todo sync
      - {"type": "result",             "data": "{...}"} — final usage summary
      - {"type": "keep_alive",         "data": ""}      — idle heartbeat
      - {"type": "done",               "data": ""}      — stream finished
      - {"type": "error",              "data": "..."}   — error
    """
    authoritative_runtime = _desktop_control_runtime
    # ADR 0059: accepted inbound bytes live only in the Attachment Store, and
    # `POST /v1/turns` is their only Desktop ingress.  This route rejects every
    # attachment-shaped input unconditionally -- including when no
    # authoritative runtime is bound -- so a Backend that somehow reached the
    # legacy branch can neither persist a second copy nor silently drop files
    # the caller believed were uploaded.
    if req.files:
        raise HTTPException(status_code=409, detail="attachments_not_supported")
    if req.file_selections or req.attachment_descriptors:
        raise HTTPException(status_code=409, detail="file_references_not_supported")
    installation_id, profile_id, source_namespace = _desktop_ingress_identity(req)
    existing_turn_id: str | None = None
    if req.job_id.strip():
        # Legacy Remote chat Jobs had a production-impossible lifecycle: the
        # canonical lifespan rejected binding after POST /jobs had already
        # persisted a queued row.  Chat state is now owned only by the Turn.
        raise HTTPException(status_code=409, detail="remote_job_binding_not_supported")
    if authoritative_runtime is not None:
        if not req.source_request_id:
            raise HTTPException(
                status_code=422,
                detail="source_request_id is required for authoritative Desktop ingress",
            )
        if req.provider_config is not None:
            raise HTTPException(
                status_code=409,
                detail="per_turn_provider_credentials_not_supported",
            )
        existing_turn_id = authoritative_runtime.lookup_ingress_turn_id(
            surface="desktop",
            source_namespace=source_namespace,
            source_request_id=req.source_request_id,
        )
    core = _get_core()
    if authoritative_runtime is not None:
        if existing_turn_id is None:
            if req.provider_id and req.provider_id != core.LLM_PROVIDER_NAME:
                raise HTTPException(
                    status_code=409,
                    detail="per_turn_provider_switch_not_supported",
                )
            if req.workspace.strip() and (
                Path(req.workspace).resolve()
                != Path(authoritative_runtime.workspace_id).resolve()
            ):
                raise HTTPException(
                    status_code=409,
                    detail="workspace_does_not_match_control_runtime",
                )
    elif req.workspace.strip():
        try:
            active_workspace = require_remote_workspace()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail="remote_workspace_unavailable"
            ) from exc
        if Path(req.workspace).resolve() != active_workspace:
            raise HTTPException(
                status_code=409,
                detail="workspace_does_not_match_backend_runtime",
            )
    session_id = req.session_id
    # Created before the Task is registered so an immediate abort can signal
    # the exact execution incarnation even before MessageEnvelope construction.
    cancel_event = threading.Event()
    active_owner: _ActiveDesktopExecution | None = None

    # If a provider config override is supplied, re-init the core.
    # NOTE: In a production multi-tenant setup you would scope this per-request
    # with a separate AsyncOpenAI client. For the desktop-app (single user) this
    # is acceptable.
    try:
        if authoritative_runtime is not None:
            pass
        elif req.provider_config and req.provider_config.provider:
            pc = req.provider_config
            provider = str(pc.provider or "").strip()
            model = str(pc.model or "").strip()
            base_url = str(pc.base_url or "").strip()
            if provider.lower() == "custom" and not base_url:
                base_url = _read_first_env("LLM_BASE_URL", "OMICSCLAW_BASE_URL")
            _validate_custom_provider_input(
                provider=provider,
                model=model,
                base_url=base_url,
                allow_env_base_url=True,
            )
            core.init(
                api_key=pc.api_key,
                base_url=base_url or None,
                model=model,
                provider=provider,
            )
        elif _chat_request_requires_provider_reinit(
            core, req.provider_id, req.model or ""
        ):
            _apply_chat_provider_switch(core, req.provider_id, req.model or "")
    except HTTPException:
        raise
    except Exception as exc:
        requested_provider = (
            (req.provider_config.provider if req.provider_config else None)
            or req.provider_id
            or ""
        )
        logger.warning(
            "Provider switch to %s failed: %s",
            requested_provider or "<unspecified>",
            exc,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Failed to switch provider to {requested_provider!r}: {exc}. "
                "Check the provider credentials and try again."
            ),
        )

    # --- Build per-request runtime overrides from frontend controls ---
    model_override = req.model or ""
    extra_api_params: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}

    if req.effort and req.effort in ("low", "medium", "high", "max"):
        extra_body["reasoning_effort"] = req.effort

    effective_model = model_override or core.OMICSCLAW_MODEL
    normalized_thinking = _build_thinking_extra_body(
        req.thinking,
        provider=core.LLM_PROVIDER_NAME,
        model=effective_model,
    )
    if normalized_thinking:
        extra_body["thinking"] = normalized_thinking

    if extra_body:
        extra_api_params["extra_body"] = extra_body

    max_tokens_override = 16384 if req.context_1m else 0

    # Attachments never reach this point: `req.files` is rejected with 409
    # above, and accepted bytes live only in the Attachment Store (ADR 0059),
    # reached through `POST /v1/turns`.  This route carries text only.
    user_content: str | list = req.content

    # asyncio.Queue bridges the dispatch() event loop (driving the
    # callbacks defined below) to the SSE generator running in the
    # response. The callbacks are still parameterless functions —
    # dispatch() yields typed events, this _run_loop dispatches each
    # event to the matching callback.
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CHAT_SSE_QUEUE_MAX_ITEMS)
    stream_observer_attached = True
    stream_detached = asyncio.Event()
    stream_done = asyncio.Event()
    streamed_text_parts: list[str] = []
    streamed_text_bytes = 0
    streamed_text_complete = True
    streamed_text_chunks = 0
    usage_totals: dict[str, float] = {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cache_read_input_tokens": 0.0,
        "cache_creation_input_tokens": 0.0,
    }
    usage_payload: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    current_policy_state = (
        _permission_profile_to_policy_state(session_id, req.permission_profile)
        if authoritative_runtime is not None
        else _set_session_permission_profile(session_id, req.permission_profile)
    )

    stream_done_signaled = False

    def _signal_stream_done() -> None:
        """End the SSE queue exactly once, even if the Task never starts.

        ``Task.cancel()`` can finish a freshly-created task before its coroutine
        enters ``_run_loop``. In that case the coroutine's ``finally`` block is
        never reached, so a task done-callback shares this idempotent signal.
        """

        nonlocal stream_done_signaled
        if stream_done_signaled:
            return
        stream_done_signaled = True
        # Completion is a separate wake-up channel, not a queue item.  It
        # therefore cannot be dropped or blocked when the bounded event queue
        # is full; the consumer drains every preceding frame before ``done``.
        stream_done.set()

    def _drain_renderer_queue() -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _detach_stream_observer() -> None:
        """Wake blocked renderer producers and release queued wire frames."""

        nonlocal stream_observer_attached
        if stream_observer_attached:
            stream_observer_attached = False
            stream_detached.set()
        _drain_renderer_queue()

    # Keep-alive interval (seconds)
    _KEEPALIVE_INTERVAL = 25

    # ---- Callbacks pushed to queue ----

    tool_call_ids_by_name: dict[str, deque[str]] = {}
    tool_progress_tasks: dict[str, asyncio.Task] = {}
    permission_request_ids: set[str] = set()
    # Live skill-log bridge. The coalescer is created per turn inside
    # ``_run_loop`` and exposed here so ``on_tool_result`` can flush pending
    # lines before a tool's result frame. Skill logs correlate by per-run id
    # (assigned in the skill chain), not by tool-call id.
    skill_log_state: dict[str, Any] = {"coalescer": None}

    slash_commands: list[dict[str, str]] = []
    try:
        from omicsclaw.surfaces.cli._constants import SLASH_COMMANDS

        slash_commands = [
            {"command": str(command), "description": str(description)}
            for command, description in SLASH_COMMANDS
        ]
    except Exception:
        slash_commands = []

    mcp_servers_meta: list[dict[str, Any]] = []
    if _mcp_load_fn is not None:
        try:
            from omicsclaw.surfaces.cli._mcp import list_mcp_servers

            mcp_servers_meta = list_mcp_servers()
        except Exception:
            mcp_servers_meta = []

    skill_names = sorted(
        alias
        for alias, info in core._skill_registry().skills.items()
        if alias == info.get("alias", alias)
    )
    effective_mode = req.mode or "ask"
    status_payload = {
        "session_id": session_id,
        "requested_model": model_override or core.OMICSCLAW_MODEL,
        "model": model_override or core.OMICSCLAW_MODEL,
        "provider": core.LLM_PROVIDER_NAME,
        "effort": req.effort or "",
        "thinking": req.thinking if req.thinking else None,
        "context_1m": req.context_1m,
        "max_tokens": max_tokens_override if max_tokens_override > 0 else 8192,
        "mode": effective_mode,
        "permission_profile": req.permission_profile or "default",
        "tools": sorted(core.get_tool_executors().keys()),
        "slash_commands": slash_commands,
        "skills": skill_names,
        "mcp_servers": mcp_servers_meta,
        "output_style": req.output_style or "",
    }

    async def _queue_event(event_type: str, data: Any) -> None:
        if not stream_observer_attached:
            return
        frame = _sse_line(event_type, data)
        try:
            queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass

        # The producer backpressures behind the renderer, but detach is an
        # independent wake-up path so a disconnected observer never strands a
        # Turn worker on a full compatibility queue.
        put_task = asyncio.create_task(queue.put(frame))
        detach_task = asyncio.create_task(stream_detached.wait())
        try:
            completed, _ = await asyncio.wait(
                {put_task, detach_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if detach_task in completed and not put_task.done():
                put_task.cancel()
            if put_task in completed:
                await put_task
        finally:
            for task in (put_task, detach_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put_task, detach_task, return_exceptions=True)
        if stream_detached.is_set():
            _drain_renderer_queue()

    def _current_tool_use_id(tool_name: str) -> str:
        pending_ids = tool_call_ids_by_name.get(tool_name)
        if pending_ids:
            return pending_ids[0]
        return ""

    def _finish_tool_progress(tool_use_id: str) -> None:
        task = tool_progress_tasks.pop(tool_use_id, None)
        if task is not None:
            task.cancel()

    async def _emit_tool_progress(tool_name: str, tool_use_id: str) -> None:
        started_at = time.monotonic()
        try:
            while True:
                elapsed = max(1, int(time.monotonic() - started_at))
                await _queue_event(
                    "tool_output",
                    json.dumps(
                        {
                            "_progress": True,
                            "tool_name": tool_name,
                            "elapsed_time_seconds": elapsed,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    def _pending_media_item_to_block(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "output_summary":
            # The collapsed "view outputs" entry: counts of the artifacts the
            # user did NOT explicitly request + a run-dir anchor. It carries no
            # file of its own — the desktop renders it as a single link.
            return {
                "type": "output_summary",
                "figures": int(item.get("figures") or 0),
                "tables": int(item.get("tables") or 0),
                "notebooks": int(item.get("notebooks") or 0),
                "runDir": str(item.get("run_dir") or item.get("runDir") or ""),
            }
        path = str(item.get("path") or item.get("localPath") or "").strip()
        if not path or not os.path.isfile(path):
            return None
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if item_type in {"photo", "image"} or mime_type.startswith("image/"):
            media_type = "image"
        elif item_type == "video" or mime_type.startswith("video/"):
            media_type = "video"
        elif item_type == "audio" or mime_type.startswith("audio/"):
            media_type = "audio"
        elif item_type in {"document", "file"}:
            media_type = "file"
        else:
            return None
        block: dict[str, Any] = {
            "type": media_type,
            "mimeType": mime_type,
            "localPath": path,
        }
        try:
            block["sizeBytes"] = os.path.getsize(path)
        except OSError:
            pass
        return block

    def _consume_pending_media_for_session() -> list[dict[str, Any]]:
        pending = getattr(core, "pending_media", None)
        if not isinstance(pending, dict):
            return []
        items = pending.pop(session_id, []) or []
        media: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            block = _pending_media_item_to_block(item)
            if not block:
                continue
            # Files dedup by path; the output_summary entry dedups by its run dir.
            key = str(block.get("localPath") or f"summary:{block.get('runDir', '')}")
            if key in seen:
                continue
            seen.add(key)
            media.append(block)
        return media

    async def on_stream_content(chunk: str):
        nonlocal streamed_text_bytes, streamed_text_complete, streamed_text_chunks
        if streamed_text_complete:
            chunk_size = utf8_size(chunk)
            if streamed_text_bytes + chunk_size <= CHAT_STREAM_COMPARISON_MAX_BYTES:
                streamed_text_parts.append(chunk)
                streamed_text_bytes += chunk_size
            else:
                # The retained text is used only to suppress a duplicate Final
                # suffix.  Once incomplete it cannot support that comparison,
                # so release it instead of accumulating an unbounded transcript
                # beside the canonical Transcript Store.
                streamed_text_parts.clear()
                streamed_text_bytes = 0
                streamed_text_complete = False
        if chunk:
            streamed_text_chunks += 1
        await _queue_event("text", chunk)

    async def on_stream_reasoning(chunk: str):
        if chunk:
            await _queue_event("thinking", chunk)

    async def on_tool_call(tool_name: str, arguments: dict):
        tool_use_id = f"call_{tool_name}_{uuid.uuid4().hex[:8]}"
        tool_call_ids_by_name.setdefault(tool_name, deque()).append(tool_use_id)
        await _queue_event(
            "tool_use",
            json.dumps(
                {
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": arguments,
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        await _queue_event("tool_output", f"Starting {tool_name}")
        tool_progress_tasks[tool_use_id] = asyncio.create_task(
            _emit_tool_progress(tool_name, tool_use_id)
        )

    def _tool_timeout_seconds(metadata: Any) -> int | None:
        if not isinstance(metadata, dict) or not metadata.get("timed_out"):
            return None
        raw_seconds = metadata.get("elapsed_seconds")
        if not isinstance(raw_seconds, (int, float)):
            return None
        return max(1, round(raw_seconds))

    def _tool_result_is_error(metadata: Any) -> bool:
        return isinstance(metadata, dict) and bool(metadata.get("is_error"))

    def _tool_result_preflight_payload(metadata: Any) -> dict | None:
        """Return the preflight ``needs_user_input`` payload when present.

        ``omicsclaw.runtime.agent.loop`` tags tool results that ended in a preflight
        confirmation request with ``preflight_pending=True`` and the
        original payload. The desktop surface uses this to render a
        dedicated prompt rather than collapsing the result as an error.
        """
        if not isinstance(metadata, dict):
            return None
        if not metadata.get("preflight_pending"):
            return None
        payload = metadata.get("preflight_payload")
        return payload if isinstance(payload, dict) else None

    async def on_tool_result(tool_name: str, display_output: Any, metadata: Any = None):
        # Flush any buffered skill logs BEFORE this tool's result frame so the
        # trailing progress lines arrive ahead of the result in the stream.
        _skill_log_coalescer = skill_log_state["coalescer"]
        if _skill_log_coalescer is not None:
            await _skill_log_coalescer.flush_now()
        content_str = str(display_output) if display_output is not None else ""
        pending_ids = tool_call_ids_by_name.get(tool_name)
        tool_use_id = pending_ids.popleft() if pending_ids else ""
        _finish_tool_progress(tool_use_id)
        timeout_seconds = _tool_timeout_seconds(metadata)
        if timeout_seconds is not None:
            await _queue_event(
                "tool_output", f"{tool_name} timed out after {timeout_seconds}s"
            )
        else:
            await _queue_event("tool_output", f"Completed {tool_name}")

        # Media is sourced SOLELY from the intent-gated `pending_media`
        # side-channel — never auto-scanned from the tool's output dir. A run's
        # figures appear in chat only when the user asked for them
        # (`return_media`); otherwise the executor queues a collapsed
        # output_summary instead (no auto-display of side-effect figures).
        media: list[dict[str, Any]] = _consume_pending_media_for_session()

        # Record which chat session produced this Run (本对话 scope) so the
        # desktop output 看板 attributes it authoritatively, instead of the
        # frontend reverse-engineering the run id from streamed media paths.
        # `media` is now exclusively the intent-gated side-channel blocks (file
        # cards + the output_summary's run-dir anchor) — every entry is a genuine
        # producer signal, and the run-dir resolver only trusts fresh run leaves,
        # so stamping straight from it is correct (and keeps figure-suppressed /
        # text-only runs linked via the summary's runDir).
        try:
            _stamp_session_for_run(session_id, display_output, media)
        except Exception:
            pass

        result_data: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "content": content_str,
        }
        if _tool_result_is_error(metadata):
            result_data["is_error"] = True
        if media:
            result_data["media"] = media

        await _queue_event(
            "tool_result",
            result_data,
        )
        preflight_payload = _tool_result_preflight_payload(metadata)
        if preflight_payload is not None:
            await _queue_event(
                "preflight_pending",
                json.dumps(
                    {
                        "tool_use_id": tool_use_id,
                        "tool_name": tool_name,
                        "session_id": session_id,
                        "payload": preflight_payload,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        if timeout_seconds is not None:
            await _queue_event(
                "tool_timeout",
                json.dumps(
                    {
                        "tool_name": tool_name,
                        "elapsed_seconds": timeout_seconds,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        if tool_name in {"task_create", "task_update", "todo_write"}:
            # Embed the full task list so the frontend 待办 panel can render live
            # without a separate fetch. All three tools now return the whole list
            # inline (fast path); the store_path reload is a defensive fallback.
            # A tool ERROR returns a plain string (no tasks/store_path) — in that
            # case we skip the event entirely so the live plan is not blanked out.
            tasks_payload: list[dict[str, Any]] | None = None
            plan_kind = "engineering_plan"
            try:
                result_obj = json.loads(content_str) if content_str else None
            except (json.JSONDecodeError, TypeError, ValueError):
                result_obj = None
            if isinstance(result_obj, dict):
                if isinstance(result_obj.get("tasks"), list):
                    tasks_payload = result_obj["tasks"]
                    plan_kind = str(result_obj.get("kind") or plan_kind)
                else:
                    store_path = str(result_obj.get("store_path") or "").strip()
                    if store_path:
                        try:
                            from omicsclaw.runtime.storage.task import TaskStore

                            store = TaskStore.load(Path(store_path))
                        except (OSError, ValueError, TypeError):
                            store = None
                        if store is not None:
                            tasks_payload = [task.to_dict() for task in store.tasks]
                            plan_kind = store.kind
            if tasks_payload is not None:
                await _queue_event(
                    "task_update",
                    json.dumps(
                        {
                            "session_id": session_id,
                            "tool_name": tool_name,
                            "kind": plan_kind,
                            "tasks": tasks_payload,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )

        if tool_name == "ask_user":
            # The ask_user tool result carries a structured question; surface it
            # to the desktop as an interactive choice card. The user answers by
            # sending their choice as the next message (the agent resumes then).
            try:
                ask_obj = json.loads(content_str) if content_str else None
            except (json.JSONDecodeError, TypeError, ValueError):
                ask_obj = None
            if isinstance(ask_obj, dict) and ask_obj.get("kind") == "ask_user":
                await _queue_event(
                    "ask_user_question",
                    json.dumps(
                        {
                            "session_id": session_id,
                            "tool_use_id": tool_use_id,
                            "question_id": ask_obj.get("question_id", ""),
                            "question": ask_obj.get("question", ""),
                            "header": ask_obj.get("header", ""),
                            "options": ask_obj.get("options", []),
                            "allow_custom": bool(ask_obj.get("allow_custom", True)),
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )

    def usage_accumulator(response_usage: Any) -> dict[str, int]:
        nonlocal usage_payload
        base_accumulator = getattr(core, "_accumulate_usage", None)
        if callable(base_accumulator):
            delta = base_accumulator(response_usage) or {}
        else:
            delta = {
                "prompt_tokens": int(getattr(response_usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(
                    getattr(response_usage, "completion_tokens", 0) or 0
                ),
                "total_tokens": int(getattr(response_usage, "total_tokens", 0) or 0),
            }
        usage_payload = _build_token_usage(
            response_usage,
            usage_totals,
            model=effective_model,
        )
        return delta

    async def request_tool_approval(
        request: Any, execution_result: Any
    ) -> dict[str, Any]:
        nonlocal current_policy_state

        tool_use_id = _current_tool_use_id(request.name)
        if tool_use_id:
            _finish_tool_progress(tool_use_id)

        current_policy_state = _permission_profile_to_policy_state(
            session_id,
            _effective_permission_profile(session_id, req.permission_profile),
        )
        _session_policy_states[session_id] = current_policy_state

        if current_policy_state.trusted:
            if tool_use_id:
                tool_progress_tasks[tool_use_id] = asyncio.create_task(
                    _emit_tool_progress(request.name, tool_use_id)
                )
            return {
                "behavior": "allow",
                "updated_input": None,
                "policy_state": current_policy_state.to_dict(),
                "persist": False,
            }

        permission_request_id = f"perm_{uuid.uuid4().hex}"
        approval_token = _permission_approval_token(
            permission_request_id,
            session_id,
            request.name,
        )
        suggestions = [
            {
                "type": "tool_permission",
                "behavior": "allow",
                "rules": [
                    {
                        "toolName": request.name,
                        "ruleContent": f"Allow `{request.name}` for this session",
                    }
                ],
            }
        ]
        payload = {
            "permissionRequestId": permission_request_id,
            "approvalToken": approval_token,
            "toolName": request.name,
            "toolInput": dict(request.arguments or {}),
            "suggestions": suggestions,
            "decisionReason": str(
                getattr(execution_result.policy_decision, "reason", "") or ""
            ),
            "blockedPath": _extract_blocked_path(dict(request.arguments or {})),
            "toolUseId": tool_use_id,
            "description": str(getattr(request.spec, "description", "") or "").strip(),
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        permission_request_ids.add(permission_request_id)
        _pending_permission_requests[permission_request_id] = {
            "future": future,
            "session_id": session_id,
            "tool_name": request.name,
            "tool_use_id": tool_use_id,
        }

        await _queue_event(
            "permission_request",
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        await _queue_event("tool_output", f"{request.name} is waiting for approval")

        try:
            decision = await future
        finally:
            permission_request_ids.discard(permission_request_id)
            _pending_permission_requests.pop(permission_request_id, None)

        current_policy_state = _permission_profile_to_policy_state(
            session_id,
            _effective_permission_profile(session_id, req.permission_profile),
        )
        _session_policy_states[session_id] = current_policy_state

        behavior = str(decision.get("behavior", "deny") or "deny").strip().lower()
        updated_input = decision.get("updated_input")
        if not isinstance(updated_input, dict):
            updated_input = None

        updated_tool_names = _tool_names_from_permission_suggestions(
            decision.get("updated_permissions")
            if isinstance(decision.get("updated_permissions"), list)
            else None
        )
        policy_state = current_policy_state
        if updated_tool_names:
            current_policy_state = _with_approved_tools(
                current_policy_state,
                updated_tool_names,
            )
            _session_policy_states[session_id] = current_policy_state
            policy_state = current_policy_state
        elif behavior == "allow":
            policy_state = _with_approved_tools(current_policy_state, {request.name})

        if behavior == "allow":
            await _queue_event("tool_output", f"Permission granted for {request.name}")
            if tool_use_id:
                tool_progress_tasks[tool_use_id] = asyncio.create_task(
                    _emit_tool_progress(request.name, tool_use_id)
                )
            return {
                "behavior": "allow",
                "updated_input": updated_input,
                "policy_state": policy_state.to_dict(),
                "persist": bool(updated_tool_names),
            }

        deny_message = str(
            decision.get("message") or f"Permission denied for `{request.name}`."
        )
        await _queue_event("tool_output", f"Permission denied for {request.name}")
        return {
            "behavior": "deny",
            "message": deny_message,
        }

    # ---- Background task running the tool loop ----

    async def _load_mcp_servers_for_worker():
        if _mcp_load_fn is None:
            return ()
        try:
            return await _mcp_load_fn()
        except Exception as mcp_exc:
            logger.warning("Failed to load MCP servers: %s", mcp_exc)
            return ()

    async def _run_loop():
        skill_log_coalescer = None
        _skill_log_token = None
        try:
            # Legacy direct dispatch still resolves MCP before its MessageEnvelope.
            # Authoritative ingress passes a lazy factory to the novel Worker so a
            # duplicate/conflict never connects to an MCP server.
            mcp_servers = ()
            if authoritative_runtime is None:
                mcp_servers = await _load_mcp_servers_for_worker()

            from omicsclaw.surfaces.desktop._compaction_event_bridge import (
                build_compaction_sse_event,
            )

            from omicsclaw.memory import desktop_chat_user_id
            from omicsclaw.runtime.agent.dispatcher import dispatch
            from omicsclaw.runtime.agent.envelope import MessageEnvelope
            from omicsclaw.runtime.agent.events import (
                ContextCompacted as _DispatchContextCompacted,
                Error as _DispatchError,
                Final as _DispatchFinal,
                PathologyDetected as _DispatchPathologyDetected,
                StreamContent as _DispatchStreamContent,
                StreamReasoning as _DispatchStreamReasoning,
                ToolCall as _DispatchToolCall,
                ToolResult as _DispatchToolResult,
            )

            # Bench (ADR 0023 decision 3): resolve thread_id = request ?? session and
            # stamp a freshly-created session so binding is durable across turns that
            # omit the field. Best-effort — never blocks a turn.
            if authoritative_runtime is not None:
                # Binding the legacy Desktop Session is a post-acceptance
                # projection, never part of duplicate/conflict inspection.
                resolved_thread_id = req.thread_id or session_id
            else:
                resolved_thread_id = await _resolve_and_bind_thread_id(
                    getattr(_get_core(), "session_manager", None),
                    desktop_chat_user_id(),
                    session_id,
                    req.thread_id,
                )

            envelope = MessageEnvelope(
                chat_id=session_id,
                content=user_content,
                user_id=desktop_chat_user_id(),
                platform="app",
                workspace=req.workspace,
                pipeline_workspace=req.pipeline_workspace,
                output_style=req.output_style,
                mcp_servers=mcp_servers,
                usage_accumulator=usage_accumulator,
                request_tool_approval=request_tool_approval,
                policy_state=current_policy_state.to_dict(),
                model_override=model_override,
                extra_api_params=extra_api_params if extra_api_params else None,
                max_tokens_override=max_tokens_override,
                system_prompt_append=req.system_prompt_append,
                mode=req.mode,
                thread_id=resolved_thread_id,
                # Bench (ADR 0020) — normalize stage once at the producer boundary
                # so a casing/whitespace mismatch ("Read", " read ") can't silently
                # bypass the stage gate; unknown values still fall through to the
                # permissive full-tool path downstream.
                stage=(req.stage or "").strip().lower(),
                cancel_event=cancel_event,
            )

            # Install the live skill-log bridge for this turn. The ContextVar
            # must be set BEFORE dispatch() so the child task it spawns copies a
            # context that already carries the emitter (see omicsclaw.skill.chain).
            from omicsclaw.skill.log_stream import skill_log_emitter_var
            from omicsclaw.surfaces.desktop._skill_log_bridge import (
                SkillLogCoalescer,
            )

            skill_log_coalescer = SkillLogCoalescer(
                loop=asyncio.get_running_loop(),
                event_sink=_queue_event,
            )
            skill_log_state["coalescer"] = skill_log_coalescer
            skill_log_coalescer.start()
            _skill_log_token = skill_log_emitter_var.set(skill_log_coalescer)

            result = ""
            terminal_error: BaseException | None = None

            async def _observe_agent_event(event) -> None:
                nonlocal result, terminal_error
                if isinstance(event, _DispatchToolCall):
                    await on_tool_call(event.tool, event.arguments)
                elif isinstance(event, _DispatchToolResult):
                    await on_tool_result(event.tool, event.result, event.metadata)
                elif isinstance(event, _DispatchStreamContent):
                    await on_stream_content(event.chunk)
                elif isinstance(event, _DispatchStreamReasoning):
                    await on_stream_reasoning(event.chunk)
                elif isinstance(event, _DispatchContextCompacted):
                    compaction_event = build_compaction_sse_event(event.payload)
                    await _queue_event(
                        compaction_event["type"], compaction_event["data"]
                    )
                elif isinstance(event, _DispatchPathologyDetected):
                    await _queue_event(
                        "pathology_detected",
                        {
                            "kind": event.kind,
                            "tool_name": event.tool_name,
                            "iteration": event.iteration,
                            "count": event.count,
                            "reason": event.reason,
                        },
                    )
                elif isinstance(event, _DispatchFinal):
                    result = event.text
                elif isinstance(event, _DispatchError):
                    if authoritative_runtime is not None:
                        terminal_error = event.exception
                    else:
                        raise event.exception

            if authoritative_runtime is not None:
                source_request_id = req.source_request_id

                surface_options = {
                    "provider_id": req.provider_id or "",
                    "effort": req.effort or "",
                    "thinking_json": (
                        json.dumps(
                            req.thinking,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if req.thinking is not None
                        else ""
                    ),
                    "context_1m": bool(req.context_1m),
                    "permission_profile": _normalize_permission_profile(
                        req.permission_profile
                    ),
                    "pipeline_workspace": req.pipeline_workspace or "",
                    "thread_id": req.thread_id or "",
                    "stage": (req.stage or "").strip().lower(),
                }
                requested_options = {
                    "mode": req.mode or "",
                    "output_style": req.output_style or "",
                    "model_override": model_override,
                    "max_tokens": max_tokens_override,
                    "system_prompt_append": req.system_prompt_append or "",
                    "surface_options": surface_options,
                }

                async def _prepare_control_content(_envelope):
                    nonlocal current_policy_state
                    # Workspace/trust/output configuration was frozen by the
                    # composition root before this ControlRuntime started.
                    # A Turn may validate that binding above, but must never
                    # re-apply mutable process configuration per request.
                    current_policy_state = _set_session_permission_profile(
                        session_id,
                        req.permission_profile,
                    )
                    await _resolve_and_bind_thread_id(
                        getattr(_get_core(), "session_manager", None),
                        desktop_chat_user_id(),
                        session_id,
                        resolved_thread_id,
                    )
                    return user_content

                async def _remember_turn(turn_id: str) -> None:
                    if active_owner is not None:
                        active_owner.turn_id = turn_id
                        # A compatibility request may omit the source id.  Once
                        # accepted, fence legacy session-based aborts with the
                        # generated identity advertised in this same frame.
                        active_owner.source_request_id = source_request_id
                        active_owner.source_namespace = source_namespace
                        current_owner = _active_sessions.get(session_id)
                        if (
                            current_owner is not None
                            and current_owner.source_request_id == source_request_id
                            and current_owner.source_namespace == source_namespace
                        ):
                            current_owner.turn_id = turn_id
                    await _queue_event(
                        "turn_accepted",
                        json.dumps(
                            {
                                "turn_id": turn_id,
                                "source_request_id": source_request_id,
                            },
                            ensure_ascii=False,
                        ),
                    )

                control_result = await authoritative_runtime.submit_and_wait(
                    RawInboundV1(
                        schema_version=1,
                        surface="desktop",
                        source_namespace=source_namespace,
                        source_request_id=source_request_id,
                        reply_target={
                            "schema_version": 1,
                            "kind": "desktop",
                            "installation_id": installation_id,
                            "profile_id": profile_id,
                            "slot": session_id,
                        },
                        content=(RawContentBlockV1(kind="text", text=req.content),),
                        requested_options=requested_options,
                    ),
                    ControlRuntimePorts(
                        response_sink=_observe_agent_event,
                        content_factory=_prepare_control_content,
                        user_id=desktop_chat_user_id(),
                        workspace=req.workspace,
                        pipeline_workspace=req.pipeline_workspace,
                        output_style=req.output_style,
                        mcp_servers_factory=_load_mcp_servers_for_worker,
                        usage_accumulator=usage_accumulator,
                        request_tool_approval=request_tool_approval,
                        policy_state=current_policy_state.to_dict(),
                        model_override=model_override,
                        extra_api_params=(
                            extra_api_params if extra_api_params else None
                        ),
                        max_tokens_override=max_tokens_override,
                        system_prompt_append=req.system_prompt_append,
                        mode=req.mode,
                        thread_id=resolved_thread_id,
                        stage=(req.stage or "").strip().lower(),
                    ),
                    on_accepted=_remember_turn,
                )
                if control_result.receipt is None:
                    raise RuntimeError(
                        "Desktop Turn was rejected: "
                        f"{control_result.acceptance.code or control_result.acceptance.status.value}"
                    )
                if control_result.receipt.status == "canceled":
                    raise asyncio.CancelledError
                if control_result.receipt.status != "succeeded":
                    error_detail = str(terminal_error or "").strip()
                    raise RuntimeError(
                        "Desktop Turn ended with "
                        f"{control_result.receipt.status}: "
                        f"{error_detail or control_result.receipt.terminal_code or 'no_terminal_code'}"
                    )
            else:
                # Direct-call compatibility Adapter for isolated tests that do
                # not enter the FastAPI lifespan. Production always owns the
                # ControlRuntime initialised above.
                async for event in dispatch(envelope):
                    await _observe_agent_event(event)
            # If the result contains text that was NOT streamed (non-streaming
            # path, or slash-command response), emit only the missing suffix.
            # queue.qsize() is not reliable here because the SSE consumer may
            # already have drained previously streamed chunks by the time the
            # tool loop returns.
            if result:
                streamed_text = (
                    "".join(streamed_text_parts) if streamed_text_complete else ""
                )
                if streamed_text_chunks == 0:
                    await _queue_event("text", result)
                elif (
                    streamed_text_complete
                    and isinstance(result, str)
                    and result.startswith(streamed_text)
                ):
                    suffix = result[len(streamed_text) :]
                    if suffix:
                        await _queue_event("text", suffix)
                elif streamed_text_complete and result != streamed_text:
                    logger.debug(
                        "Session %s returned final text differing from streamed content; "
                        "skipping replay to avoid duplicate assistant output",
                        session_id,
                    )
            await _queue_event(
                "result",
                json.dumps(
                    {
                        "usage": usage_payload,
                        "model": model_override or core.OMICSCLAW_MODEL,
                        "provider": core.LLM_PROVIDER_NAME,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        except asyncio.CancelledError:
            await _queue_event("error", "Session aborted")
        except Exception as exc:
            logger.exception("dispatch() error for session %s", session_id)
            await _queue_event("error", str(exc))
        finally:
            for permission_request_id in list(permission_request_ids):
                pending = _pending_permission_requests.pop(permission_request_id, None)
                if pending is None:
                    continue
                future = pending.get("future")
                if future is not None and not future.done():
                    future.cancel()
                permission_request_ids.discard(permission_request_id)
            for task in list(tool_progress_tasks.values()):
                task.cancel()
            tool_progress_tasks.clear()
            # Tear down the live skill-log bridge: stop emitting, drain any
            # trailing buffered lines onto the queue, then signal end-of-stream.
            if _skill_log_token is not None:
                from omicsclaw.skill.log_stream import skill_log_emitter_var

                skill_log_emitter_var.reset(_skill_log_token)
            skill_log_state["coalescer"] = None
            if skill_log_coalescer is not None:
                await skill_log_coalescer.aclose()
            _signal_stream_done()
            if active_owner is not None:
                _release_active_execution(session_id, active_owner)

    # ---- SSE generator ----

    async def event_generator():
        nonlocal active_owner

        task = asyncio.create_task(_run_loop())
        owner = _ActiveDesktopExecution(
            task=task,
            cancel_event=cancel_event,
            source_request_id=req.source_request_id,
            source_namespace=source_namespace,
        )
        active_owner = owner
        # Covers cancellation after registration but before ``_run_loop`` gets
        # its first event-loop turn (and therefore before its finally exists).
        task.add_done_callback(lambda _task: _signal_stream_done())
        # Register before exposing the first SSE frame. An immediate interrupt
        # can now always target this exact owner instead of racing a 404 window.
        _replace_active_execution(session_id, owner)

        try:
            yield _sse_line("status", status_payload)
            yield _sse_line("mode_changed", effective_mode)

            while True:
                if stream_done.is_set() and queue.empty():
                    yield _sse_done()
                    break

                get_task = asyncio.create_task(queue.get())
                done_task = asyncio.create_task(stream_done.wait())
                completed: set[asyncio.Task]
                try:
                    completed, _ = await asyncio.wait(
                        {get_task, done_task},
                        timeout=_KEEPALIVE_INTERVAL,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except BaseException:
                    get_task.cancel()
                    done_task.cancel()
                    await asyncio.gather(get_task, done_task, return_exceptions=True)
                    raise

                if not completed:
                    get_task.cancel()
                    done_task.cancel()
                    await asyncio.gather(get_task, done_task, return_exceptions=True)
                    yield _sse_line("keep_alive", "")
                    continue

                if get_task in completed:
                    frame = get_task.result()
                    if not done_task.done():
                        done_task.cancel()
                    await asyncio.gather(done_task, return_exceptions=True)
                    yield frame
                    continue

                # Completion can win the race only after all producers have
                # finished awaited queue writes.  Prefer a concurrently-ready
                # frame, otherwise the queue is now durably empty.
                if not get_task.done():
                    get_task.cancel()
                await asyncio.gather(get_task, return_exceptions=True)
                try:
                    frame = queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                else:
                    yield frame
                    continue
                yield _sse_done()
                break
        except asyncio.CancelledError:
            # A ControlRuntime-backed client disconnect detaches only this
            # observer. Lifecycle cancellation is a separate Turn operation.
            _detach_stream_observer()
            coalescer = skill_log_state.get("coalescer")
            if coalescer is not None:
                coalescer.detach()
            if _desktop_control_runtime is None:
                _cancel_active_execution(owner)
            yield _sse_error("Client disconnected")
            yield _sse_done()
        finally:
            if not owner.task.done():
                _detach_stream_observer()
                if _desktop_control_runtime is not None:
                    coalescer = skill_log_state.get("coalescer")
                    if coalescer is not None:
                        coalescer.detach()
                else:
                    _cancel_active_execution(owner)
            _release_active_execution(session_id, owner)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /chat/abort — cancel a running session
# ---------------------------------------------------------------------------


def _abort_active_session(
    session_id: str,
    expected_source_request_id: str | None = None,
) -> str:
    """Cancel only the active execution incarnation selected by the caller.

    Modern Desktop clients must supply the ``source_request_id`` used to start
    the stream. A missing generation remains compatible only with a genuinely
    legacy owner whose source id is empty; it may never cancel a modern owner.
    """

    owner = _active_sessions.get(session_id)
    if owner is None:
        return _ABORT_NOT_FOUND

    expected = str(expected_source_request_id or "")
    if expected:
        if owner.source_request_id != expected:
            return _ABORT_GENERATION_MISMATCH
    elif owner.source_request_id:
        return _ABORT_GENERATION_REQUIRED

    if not _cancel_active_execution(owner):
        return _ABORT_TURN_UNRESOLVED
    _release_active_execution(session_id, owner)
    return _ABORTED


@app.post("/chat/abort", dependencies=[Depends(require_bearer_token)])
async def chat_abort(req: AbortRequest):
    """Cancel a running dispatch() session (see ``_abort_active_session``)."""
    result = _abort_active_session(req.session_id, req.source_request_id)
    if result == _ABORT_NOT_FOUND:
        raise HTTPException(404, detail="No active session with that ID")
    if result == _ABORT_GENERATION_MISMATCH:
        raise HTTPException(409, detail="stale_stream_generation")
    if result == _ABORT_GENERATION_REQUIRED:
        raise HTTPException(409, detail="stream_generation_required")
    if result == _ABORT_TURN_UNRESOLVED:
        raise HTTPException(409, detail="canonical_turn_not_yet_bound")
    return {"status": "aborted", "session_id": req.session_id}


def _desktop_control_runtime_required() -> ControlRuntime:
    if _desktop_control_runtime is None:
        raise HTTPException(503, detail="Desktop ControlRuntime is not ready")
    return _desktop_control_runtime


def _desktop_run_runtime_required() -> RunRuntime:
    if _desktop_run_runtime is None or not _desktop_run_runtime.lifecycle_ready:
        raise HTTPException(503, detail="Desktop RunRuntime is not ready")
    return _desktop_run_runtime


def _require_desktop_idempotency_key(value: str | None) -> str:
    key = str(value or "")
    if len(key) != 32 or any(character not in "0123456789abcdef" for character in key):
        raise HTTPException(
            422,
            detail="Idempotency-Key must be 32 lowercase hexadecimal characters",
        )
    return key


def _raise_desktop_turn_rejection(status, code: str) -> None:
    detail = code or "turn_rejected"
    if status is TurnAcceptanceStatus.CONFLICT:
        raise HTTPException(409, detail=detail)
    status_code = {
        "project_not_found": 404,
        "conversation_not_found": 404,
        "conversation_address_mismatch": 409,
        "project_archived": 409,
        "proposed_conversation_id_conflict": 409,
        "proposed_turn_id_conflict": 409,
        "turn_backpressure": 429,
        "admission_contention": 429,
        "delivery_backpressure": 503,
        "turn_execution_unavailable": 503,
        "control_not_ready": 503,
        "attachments_not_supported": 503,
    }.get(detail, 422)
    raise HTTPException(status_code, detail=detail)


def _turn_accepted_v1_payload(result) -> dict[str, Any]:
    receipt = result.receipt
    if receipt is None:
        raise RuntimeError("accepted Desktop submission has no durable Receipt")
    return DesktopTurnAcceptedV1(
        schema_version=1,
        turn_id=receipt.turn_id,
        conversation_id=receipt.conversation_id,
        status=receipt.status,
        duplicate=result.acceptance.status is TurnAcceptanceStatus.DUPLICATE,
        receipt_revision=receipt.revision,
        accepted_at_ms=receipt.created_at_ms,
        attachments=tuple(
            DesktopAttachmentReferenceV1(
                schema_version=reference.schema_version,
                attachment_id=reference.attachment_id,
                ordinal=reference.ordinal,
                content_sha256=reference.content_sha256,
                byte_size=reference.byte_size,
                display_name=reference.display_name,
                media_type=reference.media_type,
            )
            for reference in result.attachment_refs
        ),
    ).model_dump()


@app.post(
    "/v1/turns",
    dependencies=[Depends(require_bearer_token)],
    status_code=202,
    response_model=DesktopTurnAcceptedV1,
    responses={
        200: {
            "model": DesktopTurnAcceptedV1,
            "description": "Matching idempotent duplicate; no file part is read.",
        },
        400: {"description": "Malformed multipart transport or JSON encoding."},
        408: {"description": "Multipart body read deadline exceeded."},
        409: {"description": "Idempotency or authoritative state conflict."},
        413: {"description": "Multipart transport or request document too large."},
        415: {"description": "multipart/form-data is required."},
        422: {"description": "Manifest, attachment, or Idempotency-Key rejected."},
        429: {"description": "Multipart or Turn admission backpressure."},
        503: {"description": "Authoritative runtime is not ready."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["request"],
                        "properties": {
                            "request": {
                                "type": "string",
                                "contentMediaType": "application/json",
                                "description": "DesktopTurnSubmissionV1 JSON document.",
                            }
                        },
                        "additionalProperties": {
                            "type": "string",
                            "format": "binary",
                            "description": (
                                "One file part named by each source_attachment_id."
                            ),
                        },
                    },
                    "encoding": {"request": {"contentType": "application/json"}},
                }
            },
        }
    },
)
async def submit_desktop_turn_v1(
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Durably accept one bounded Desktop multipart image Turn.

    The configured conditional bearer gate runs before this handler (local
    loopback mode may intentionally have no token). Multipart parsing is manual
    so the Adapter can cap the raw request stream and concurrent spool count.
    """

    header_keys = request.headers.getlist("idempotency-key")
    if len(header_keys) > 1:
        raise HTTPException(422, detail="exactly one Idempotency-Key is required")
    if header_keys and idempotency_key != header_keys[0]:
        raise HTTPException(422, detail="Idempotency-Key is ambiguous")
    source_request_id = _require_desktop_idempotency_key(idempotency_key)
    runtime = _desktop_control_runtime_required()
    lease = _desktop_multipart_capacity.try_acquire()
    if lease is None:
        raise HTTPException(429, detail="desktop_multipart_backpressure")

    upload = None
    try:
        try:
            async with asyncio.timeout(DEFAULT_MULTIPART_READ_TIMEOUT_SECONDS):
                upload = await decode_desktop_multipart_submission(
                    request,
                    max_attachments=runtime.attachment_store.max_attachments,
                    max_batch_bytes=runtime.attachment_store.max_batch_bytes,
                )
        except TimeoutError as exc:
            raise HTTPException(408, detail="multipart_read_timeout") from exc
        except DesktopMultipartError as exc:
            raise HTTPException(exc.status_code, detail=exc.code) from exc

        raw = upload.submission.to_raw_inbound(
            source_request_id=source_request_id,
            source_namespace="desktop/v1/local/owner",
            reply_target={
                "schema_version": 1,
                "kind": "desktop",
                "installation_id": "local",
                "profile_id": "owner",
                "slot": "main",
            },
        )
        from omicsclaw.memory import desktop_chat_user_id

        result = await runtime.submit(
            raw,
            ControlRuntimePorts(
                user_id=desktop_chat_user_id(),
                workspace=runtime.workspace_id,
                policy_state=ToolPolicyState(surface="app").to_dict(),
            ),
            attachment_source=upload.source,
        )
        if result.acceptance.status not in {
            TurnAcceptanceStatus.ACCEPTED,
            TurnAcceptanceStatus.DUPLICATE,
        }:
            _raise_desktop_turn_rejection(
                result.acceptance.status,
                result.acceptance.code,
            )
        status_code = (
            200 if result.acceptance.status is TurnAcceptanceStatus.DUPLICATE else 202
        )
        return JSONResponse(
            _turn_accepted_v1_payload(result),
            status_code=status_code,
            headers={"Location": f"/v1/turns/{result.acceptance.turn_id}"},
        )
    finally:
        try:
            if upload is not None:
                try:
                    await upload.aclose()
                except asyncio.CancelledError:
                    # Release transport capacity even if the request task is
                    # canceled while closing spools; finish the close in a
                    # detached task before propagating cancellation.
                    if not upload.source.closed:
                        cleanup = asyncio.create_task(upload.aclose())
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            pass
                    raise
                except Exception as exc:  # pragma: no cover - OS cleanup failure
                    logger.warning("Desktop multipart spool cleanup failed: %s", exc)
        finally:
            lease.release()


def _turn_receipt_payload(runtime: ControlRuntime, turn_id: str) -> dict[str, Any]:
    try:
        snapshot = runtime.get_turn_snapshot(turn_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, detail="Turn not found") from exc
    return desktop_turn_receipt_v1(snapshot).model_dump(mode="python")


class _TurnEventStreamResponse(StreamingResponse):
    """Advertise the actual SSE media type in OpenAPI and on the wire."""

    media_type = "text/event-stream"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Release the observer on every ASGI exit, including send failure."""

        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


@app.get(
    "/v1/turns/{turn_id}",
    dependencies=[Depends(require_bearer_token)],
    response_model=DesktopTurnReceiptV1,
)
@app.get(
    "/turns/{turn_id}",
    dependencies=[Depends(require_bearer_token)],
    response_model=DesktopTurnReceiptV1,
)
async def get_turn_receipt(turn_id: str):
    """Read a durable Receipt without submitting or replaying its Turn."""

    return _turn_receipt_payload(_desktop_control_runtime_required(), turn_id)


def _parse_turn_event_cursor(last_event_id: str | None) -> int:
    if last_event_id in {None, ""}:
        return 0
    if last_event_id == "-1":
        # Compatibility for the pre-one-based internal sentinel.
        return -1
    if (
        not last_event_id.isascii()
        or not last_event_id.isdigit()
        or len(last_event_id) > 20
        or int(last_event_id) > (2**64 - 1)
    ):
        raise ValueError
    return int(last_event_id)


@app.get(
    "/v1/turns/{turn_id}/events",
    dependencies=[Depends(require_bearer_token)],
    response_class=_TurnEventStreamResponse,
)
@app.get(
    "/turns/{turn_id}/events",
    dependencies=[Depends(require_bearer_token)],
    response_class=_TurnEventStreamResponse,
)
async def observe_turn_events(
    turn_id: str,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    """Reconnect to the bounded process-local Event stream of an existing Turn."""

    runtime = _desktop_control_runtime_required()
    # Classic FastAPI parameter declarations keep the supported 0.100 floor
    # importable. Direct handler-level tests see the Header sentinel itself.
    if not isinstance(last_event_id, (str, type(None))):
        last_event_id = None
    try:
        after_sequence = _parse_turn_event_cursor(last_event_id)
    except ValueError as exc:
        raise HTTPException(
            400,
            detail="Last-Event-ID must be -1 or a non-negative integer",
        ) from exc
    try:
        observation = runtime.open_turn_observation(
            turn_id,
            after_sequence=after_sequence,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, detail="Turn not found") from exc
    except EventHubCapacityError as exc:
        raise HTTPException(429, detail="turn_observer_capacity_exceeded") from exc
    if observation.gap is not None and observation.gap.reason == "cursor_ahead":
        await observation.aclose()
        raise HTTPException(
            400,
            detail="Last-Event-ID is ahead of the current Turn Event sequence",
        )

    try:
        body = DesktopTurnSSEBody(
            observation,
            requested_after_sequence=after_sequence,
        )
    except BaseException:
        await observation.aclose()
        raise
    return _TurnEventStreamResponse(
        body,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(body.aclose),
    )


@app.post(
    "/v1/turns/{turn_id}/cancel",
    dependencies=[Depends(require_bearer_token)],
    response_model=DesktopTurnCancelResultV1,
)
@app.post(
    "/turns/{turn_id}/cancel",
    dependencies=[Depends(require_bearer_token)],
    response_model=DesktopTurnCancelResultV1,
)
async def cancel_turn(turn_id: str):
    """Explicit lifecycle cancellation by opaque Turn ID."""

    runtime = _desktop_control_runtime_required()
    result = runtime.cancel(turn_id)
    if result.code == "turn_not_found":
        raise HTTPException(404, detail="Turn not found")
    if not result.changed and result.code not in {
        "already_terminal",
        "cancel_already_requested",
    }:
        raise HTTPException(409, detail=result.code)
    snapshot = runtime.get_turn_snapshot(turn_id)
    return DesktopTurnCancelResultV1(
        schema_version=1,
        turn_id=turn_id,
        changed=result.changed,
        code=result.code,
        receipt=desktop_turn_receipt_v1(snapshot),
    ).model_dump(mode="python")


def _raise_desktop_run_rejection(status, code: str) -> None:
    detail = code or "run_rejected"
    if status is RunAcceptanceStatus.CONFLICT:
        raise HTTPException(409, detail=detail)
    status_code = {
        "project_not_found": 404,
        "parent_turn_not_found": 404,
        "skill_not_found": 404,
        "project_archived": 409,
        "skill_not_canonical": 422,
        "skill_deprecated": 422,
        "skill_demo_not_supported": 422,
        "run_kind_not_supported": 422,
        "resource_contract_missing": 422,
        "resource_contract_mismatch": 422,
        "resource_unsupported": 422,
        "skill_authority_unavailable": 503,
        "run_backpressure": 429,
        "admission_contention": 429,
        "control_not_ready": 503,
        "executor_isolation_unavailable": 503,
    }.get(detail, 422)
    raise HTTPException(status_code, detail=detail)


@app.post(
    "/v1/runs",
    dependencies=[Depends(require_bearer_token)],
    status_code=202,
    response_model=DesktopRunAcceptedV1,
    responses={
        200: {
            "model": DesktopRunAcceptedV1,
            "description": "Matching idempotent duplicate; no work is admitted.",
        },
        400: {"description": "Malformed JSON transport or encoding."},
        401: {"description": "Bearer authentication failed when configured."},
        404: {"description": "Canonical Skill, Project, or parent Turn not found."},
        408: {"description": "JSON body read deadline exceeded."},
        409: {"description": "Run Submission ID or authoritative state conflict."},
        413: {"description": "JSON transport exceeded the fixed byte limit."},
        415: {"description": "A plain application/json body is required."},
        422: {"description": "Run request or static execution contract rejected."},
        429: {"description": "Bounded Run admission backpressure."},
        503: {"description": "Authoritative Run runtime is unavailable."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": DesktopRunSubmissionV1.model_json_schema()
                }
            },
        }
    },
)
async def submit_desktop_run_v1(
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Durably admit one strict demo-only Simple Skill Run."""

    header_keys = request.headers.getlist("idempotency-key")
    if len(header_keys) != 1 or idempotency_key != header_keys[0]:
        raise HTTPException(422, detail="exactly one Idempotency-Key is required")
    run_submission_id = _require_desktop_idempotency_key(idempotency_key)
    try:
        submission = await decode_desktop_run_submission(request)
    except DesktopRunWireError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from exc
    runtime = _desktop_run_runtime_required()
    result = await runtime.submit(submission.to_domain(run_submission_id))
    if result.acceptance_status not in {
        RunAcceptanceStatus.ACCEPTED,
        RunAcceptanceStatus.DUPLICATE,
    }:
        _raise_desktop_run_rejection(result.acceptance_status, result.code)
    receipt = result.receipt
    if receipt is None:
        raise HTTPException(503, detail="accepted_run_receipt_unavailable")
    payload = DesktopRunAcceptedV1(
        schema_version=1,
        run_id=receipt.run_id,
        status=receipt.status,
        duplicate=result.acceptance_status is RunAcceptanceStatus.DUPLICATE,
        receipt_revision=receipt.revision,
        accepted_at_ms=receipt.created_at_ms,
    )
    status_code = (
        200 if result.acceptance_status is RunAcceptanceStatus.DUPLICATE else 202
    )
    return JSONResponse(
        payload.model_dump(mode="python"),
        status_code=status_code,
        headers={"Location": f"/v1/runs/{receipt.run_id}"},
    )


@app.get(
    "/v1/runs/{run_id}",
    dependencies=[Depends(require_bearer_token)],
    response_model=DesktopRunReceiptV1,
    responses={
        401: {"description": "Bearer authentication failed when configured."},
        404: {"description": "Run not found."},
        503: {"description": "Authoritative Run runtime is unavailable."},
    },
)
async def get_run_receipt_v1(run_id: str):
    """Observe an existing durable Run without enqueue, Lease, or Assignment."""

    try:
        observation = _desktop_run_runtime_required().get_receipt(run_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, detail="Run not found") from exc
    return desktop_run_receipt_v1(observation).model_dump(mode="python")


@app.post(
    "/v1/runs/{run_id}/cancel",
    dependencies=[Depends(require_bearer_token)],
    response_model=DesktopRunCancelResultV1,
    responses={
        401: {"description": "Bearer authentication failed when configured."},
        404: {"description": "Run not found."},
        409: {"description": "Run cannot accept the cancellation command."},
        503: {"description": "Authoritative Run runtime is unavailable."},
    },
)
async def cancel_run_v1(run_id: str):
    """Explicitly cancel one canonical Run by opaque Run ID."""

    try:
        result = await _desktop_run_runtime_required().cancel(run_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, detail="Run not found") from exc
    if result.code not in {
        "canceled_before_assignment",
        "cancel_requested",
        "cancel_already_requested",
        "already_terminal",
    }:
        raise HTTPException(409, detail=result.code)
    return DesktopRunCancelResultV1(
        schema_version=1,
        run_id=run_id,
        changed=result.changed,
        code=result.code,
        receipt=desktop_run_receipt_from_record(result.receipt),
    ).model_dump(mode="python")


@app.get(
    "/v1/run-integrity-incidents",
    dependencies=[Depends(require_bearer_token)],
    response_model=DesktopRunIntegrityIncidentPageV1,
    responses={
        400: {"description": "The incident cursor is unknown or incompatible."},
        401: {"description": "Bearer authentication failed when configured."},
        422: {"description": "Query parameters are outside the closed V1 shape."},
        503: {"description": "Authoritative Run runtime is unavailable."},
    },
)
async def list_run_integrity_incidents_v1(
    run_id: Annotated[
        str | None,
        Query(pattern=r"^[0-9a-f]{32}$", max_length=32),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(pattern=r"^[0-9a-f]{32}$", max_length=32),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=DESKTOP_RUN_INCIDENT_MAX_PAGE_SIZE),
    ] = 50,
):
    """Observe content-free durable incidents without touching execution state."""

    try:
        page = _desktop_run_runtime_required().list_integrity_incidents(
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(400, detail="invalid_incident_cursor") from exc
    return desktop_run_integrity_incident_page_v1(page).model_dump(mode="json")


@app.post("/chat/permission", dependencies=[Depends(require_bearer_token)])
async def chat_permission(req: PermissionResponseRequest):
    """Resolve a pending chat permission request."""
    pending = _pending_permission_requests.get(req.permissionRequestId)
    if pending is None:
        return {
            "ok": False,
            "permissionRequestId": req.permissionRequestId,
            "status": "expired",
        }

    expected_token = _permission_approval_token(
        req.permissionRequestId,
        str(pending.get("session_id", "")),
        str(pending.get("tool_name", "")),
    )
    if not hmac.compare_digest(req.approvalToken, expected_token):
        return {
            "ok": False,
            "permissionRequestId": req.permissionRequestId,
            "status": "invalid_token",
        }

    future = pending.get("future")
    if future is None or future.done():
        return {
            "ok": False,
            "permissionRequestId": req.permissionRequestId,
            "status": "resolved",
        }

    decision = req.decision or {}
    behavior = str(decision.get("behavior", "deny") or "deny").strip().lower()
    payload = {
        "behavior": "allow" if behavior == "allow" else "deny",
        "updated_input": (
            decision.get("updatedInput")
            if isinstance(decision.get("updatedInput"), dict)
            else None
        ),
        "updated_permissions": (
            decision.get("updatedPermissions")
            if isinstance(decision.get("updatedPermissions"), list)
            else []
        ),
        "message": str(decision.get("message", "") or "").strip(),
    }
    future.set_result(payload)
    return {
        "ok": True,
        "permissionRequestId": req.permissionRequestId,
        "behavior": payload["behavior"],
        "session_id": pending.get("session_id", ""),
    }


@app.post(
    "/chat/session-permission-profile",
    dependencies=[Depends(require_bearer_token)],
)
async def chat_session_permission_profile(req: SessionPermissionProfileRequest):
    """Update the live permission profile for a session and release pending approvals."""
    policy_state = _set_session_permission_profile(
        req.session_id, req.permission_profile
    )
    profile = _effective_permission_profile(req.session_id, req.permission_profile)
    auto_approved_requests = 0

    if profile == "full_access":
        for pending in list(_pending_permission_requests.values()):
            if pending.get("session_id") != req.session_id:
                continue

            future = pending.get("future")
            if future is None or future.done():
                continue

            future.set_result(
                {
                    "behavior": "allow",
                    "updated_input": None,
                    "updated_permissions": [],
                    "message": "",
                }
            )
            auto_approved_requests += 1

    return {
        "ok": True,
        "session_id": req.session_id,
        "permission_profile": profile,
        "active": req.session_id in _active_sessions,
        "auto_approved_requests": auto_approved_requests,
        "policy_state": policy_state.to_dict(),
    }


# ---------------------------------------------------------------------------
# GET /workspace — current runtime workspace configuration
# ---------------------------------------------------------------------------


@app.get("/workspace", dependencies=[Depends(require_bearer_token)])
async def get_workspace():
    try:
        active_workspace = require_remote_workspace()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="remote_workspace_unavailable"
        ) from exc
    core = _get_core()
    trusted_dirs = [str(d) for d in getattr(core, "TRUSTED_DATA_DIRS", [])]
    return {
        # ``workspace`` is retained as the compatibility alias consumed by the
        # current App.  ``active_workspace`` names the actual authority.
        "workspace": str(active_workspace),
        "active_workspace": str(active_workspace),
        "restart_required": False,
        "trusted_dirs": trusted_dirs,
    }


# ---------------------------------------------------------------------------
# PUT /workspace — sync default project directory from frontend
# ---------------------------------------------------------------------------


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workspace: str


_WORKSPACE_REQUEST_MAX_BYTES = 4 * 1024
_WORKSPACE_REQUEST_READ_TIMEOUT_SECONDS = 5.0


def _workspace_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate_workspace_json_key")
        document[key] = value
    return document


def _reject_workspace_json_constant(value: str) -> None:
    raise ValueError(f"invalid_workspace_json_constant:{value}")


async def _decode_workspace_request(request: Request) -> WorkspaceRequest:
    """Count and strictly decode the tiny Workspace command body.

    The route deliberately accepts ``Request`` rather than a Pydantic body
    parameter so FastAPI can complete bearer authentication before reading or
    validating attacker-controlled bytes.
    """

    raw_content_type = request.headers.get("content-type", "")
    media_type, _, raw_parameters = raw_content_type.partition(";")
    if media_type.strip().lower() != "application/json":
        raise HTTPException(415, detail="application_json_required")
    parameters = [
        parameter.strip().lower()
        for parameter in raw_parameters.split(";")
        if parameter.strip()
    ]
    if len(parameters) > 1 or any(
        not parameter.startswith("charset=")
        or parameter.removeprefix("charset=").strip('"') not in {"utf-8", "utf8"}
        for parameter in parameters
    ):
        raise HTTPException(415, detail="utf8_json_required")
    if request.headers.get("content-encoding", "").strip().lower() not in {
        "",
        "identity",
    }:
        raise HTTPException(415, detail="content_encoding_not_supported")

    declared_lengths = request.headers.getlist("content-length")
    if len(declared_lengths) > 1:
        raise HTTPException(400, detail="invalid_content_length")
    if declared_lengths:
        declared = declared_lengths[0]
        if not declared.isascii() or not declared.isdigit():
            raise HTTPException(400, detail="invalid_content_length")
        if int(declared, 10) > _WORKSPACE_REQUEST_MAX_BYTES:
            raise HTTPException(413, detail="workspace_request_too_large")

    async def _read_counted_body() -> bytes:
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > _WORKSPACE_REQUEST_MAX_BYTES:
                    raise HTTPException(413, detail="workspace_request_too_large")
                body.extend(chunk)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, detail="workspace_request_read_failed") from exc
        return bytes(body)

    try:
        raw_body = await asyncio.wait_for(
            _read_counted_body(),
            timeout=_WORKSPACE_REQUEST_READ_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise HTTPException(408, detail="workspace_request_read_timeout") from exc
    try:
        document_text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, detail="invalid_workspace_json_encoding") from exc
    try:
        document = json.loads(
            document_text,
            object_pairs_hook=_workspace_json_object,
            parse_constant=_reject_workspace_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise HTTPException(400, detail="invalid_workspace_json") from exc
    if not isinstance(document, dict):
        raise HTTPException(400, detail="invalid_workspace_json")
    try:
        return WorkspaceRequest.model_validate(document)
    except (ValidationError, RecursionError) as exc:
        raise HTTPException(422, detail="invalid_workspace_request") from exc


@app.put("/workspace", dependencies=[Depends(require_bearer_token)])
async def set_workspace(request: Request):
    """Confirm the active Workspace or require an explicit Backend restart.

    A Workspace is composition input frozen before ``ControlRuntime`` and
    ``RunRuntime`` start.  Mutating environment variables or output/trust
    roots here would create two authorities inside one Backend lifespan.
    """
    req = await _decode_workspace_request(request)
    try:
        active_workspace = require_remote_workspace()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="remote_workspace_unavailable"
        ) from exc

    ws = req.workspace.strip()
    if not ws:
        raise HTTPException(400, detail="workspace is required")
    ws_path = Path(ws).expanduser()
    if not ws_path.is_absolute():
        raise HTTPException(400, detail="workspace must be an absolute path")
    try:
        # Existence is validated by the next Backend lifespan, not by this
        # no-mutation command.  Comparing first makes every different absolute
        # root one deterministic restart-required response and keeps same-root
        # confirmation idempotent even if external deletion occurs meanwhile.
        requested_workspace = ws_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, detail="invalid workspace path") from exc
    if requested_workspace != active_workspace:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "workspace_change_requires_backend_restart",
                "workspace": str(active_workspace),
                "active_workspace": str(active_workspace),
                "requested_workspace": str(requested_workspace),
                "restart_required": True,
            },
        )

    core = _get_core()
    trusted_dirs = [str(d) for d in getattr(core, "TRUSTED_DATA_DIRS", [])]
    output_dir = getattr(core, "OUTPUT_DIR", None)
    return {
        "ok": True,
        "workspace": str(active_workspace),
        "active_workspace": str(active_workspace),
        "requested_workspace": str(requested_workspace),
        "restart_required": False,
        "trusted_dirs": trusted_dirs,
        "workspace_env": str(os.environ.get("OMICSCLAW_WORKSPACE", "") or ""),
        "output_dir": str(output_dir) if output_dir is not None else "",
    }


# ---------------------------------------------------------------------------
# GET /files/browse — directory browser for remote-runtime folder pickers
# ---------------------------------------------------------------------------
#
# Desktop clients need to let the user pick a workspace directory that
# lives on THIS host (the backend's filesystem), not on the client's
# local machine. The App's local `/api/files/browse` only works for
# co-located backends; when the backend is on a remote SSH runtime the
# App proxies here instead.
#
# Read-only. Authorization is the same bearer-token middleware the rest
# of the app_server already enforces, plus OS filesystem permissions
# (we can only surface what the backend process itself can read). The
# endpoint deliberately does NOT consult TRUSTED_DATA_DIRS — the user
# is in the middle of PICKING a workspace, so gating by trust would
# prevent the first-time-pick flow. `PUT /workspace` does the
# is_dir() + absolute-path validation when the choice is committed.


@app.get("/files/browse", dependencies=[Depends(require_bearer_token)])
async def browse_directories(path: Optional[str] = None):
    """List subdirectories under `path` (or $HOME when omitted).

    Response shape matches the App's existing local
    `/api/files/browse` route so `<FolderPicker/>` can consume either
    implementation interchangeably.
    """
    base_raw = (path or "").strip()
    try:
        base = Path(base_raw).expanduser() if base_raw else Path.home()
        base = base.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="directory does not exist") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, detail=f"invalid path: {exc}") from exc

    if not base.is_dir():
        raise HTTPException(400, detail="path is not a directory")

    directories: list[dict[str, Any]] = []
    try:
        entries = list(base.iterdir())
    except PermissionError as exc:
        raise HTTPException(403, detail="permission denied") from exc

    for entry in entries:
        # Skip dotfiles to match the App's local implementation — users
        # expect the same filter on both sides, and cluttered pickers
        # get ignored.
        if entry.name.startswith("."):
            continue
        is_symlink = entry.is_symlink()
        try:
            is_dir = entry.is_dir()  # follows symlinks
        except OSError:
            continue
        if not is_dir:
            continue
        item: dict[str, Any] = {
            "name": entry.name,
            "path": str(entry),
            "isSymbolicLink": is_symlink,
        }
        if is_symlink:
            try:
                item["targetPath"] = str(entry.resolve())
            except OSError:
                # Broken symlink — skip rather than return a bogus entry.
                continue
        directories.append(item)

    directories.sort(key=lambda d: d["name"].lower())

    parent_path = str(base.parent) if str(base.parent) != str(base) else None
    return {
        "current": str(base),
        "parent": parent_path,
        "directories": directories,
    }


def _classify_tree_file(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "path": str(path),
        "type": "file",
        "size": path.stat().st_size,
        "extension": path.suffix,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _trusted_file_roots() -> list[Path]:
    core = _get_core()
    roots: list[Path] = []
    for raw in getattr(core, "TRUSTED_DATA_DIRS", []) or []:
        try:
            roots.append(Path(raw).expanduser().resolve(strict=True))
        except OSError:
            continue
    output_dir = getattr(core, "OUTPUT_DIR", None)
    if output_dir:
        try:
            roots.append(Path(output_dir).expanduser().resolve(strict=True))
        except OSError:
            pass
    try:
        roots.append(require_remote_workspace().resolve(strict=True))
    except (OSError, RuntimeError):
        pass
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _recognized_output_run_root(path: Path) -> Path | None:
    """Return the canonical Run containing ``path`` by its lexical location.

    The lexical check matters for a symlink entry: resolving the candidate
    first would erase the fact that an escaping alias was presented from
    inside a Run.  Generic trusted input trees deliberately return ``None``.
    """
    try:
        core = _get_core()
    except RuntimeError:
        return None
    output_dir = getattr(core, "OUTPUT_DIR", None)
    if not output_dir:
        return None

    from omicsclaw.common import run_paths

    output_root = Path(output_dir).expanduser()
    candidate = Path(os.path.abspath(Path(path).expanduser()))
    candidate_locations = {candidate}
    try:
        candidate_locations.add(Path(path).expanduser().resolve(strict=True))
    except (OSError, RuntimeError):
        pass
    try:
        canonical_output_root = output_root.resolve(strict=True)
        lexical_output_root = Path(os.path.abspath(output_root))
        if not any(
            _is_relative_to(location, root)
            for location in candidate_locations
            for root in (lexical_output_root, canonical_output_root)
        ):
            return None
        runs = run_paths.iter_run_dirs(output_root)
        for _, run_dir in runs:
            if run_dir.is_symlink():
                continue
            try:
                canonical_run = run_dir.resolve(strict=True)
                canonical_run.relative_to(canonical_output_root)
            except (OSError, RuntimeError, ValueError):
                continue
            lexical_roots = {
                Path(os.path.abspath(run_dir)),
                canonical_run,
            }
            if any(
                _is_relative_to(location, root)
                for location in candidate_locations
                for root in lexical_roots
            ):
                return canonical_run
    except OSError:
        return None
    return None


def _resolve_trusted_file_path(raw_path: str) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise HTTPException(400, detail="path is required")
    candidate = Path(raw).expanduser()
    try:
        target = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="file does not exist") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, detail=f"invalid path: {exc}") from exc
    if not target.is_file():
        raise HTTPException(400, detail="path is not a file")
    trusted_roots = _trusted_file_roots()
    if not any(_is_relative_to(target, root) for root in trusted_roots):
        raise HTTPException(403, detail="access denied")
    run_root = _recognized_output_run_root(candidate)
    if run_root is not None and not is_scientific_output_file(
        candidate,
        output_root=run_root,
        claim_identities=collect_output_claim_identities(run_root),
    ):
        raise HTTPException(404, detail="file does not exist")
    return target


def _scan_file_tree(
    base: Path,
    depth: int,
    visited: set[Path] | None = None,
    *,
    run_root: Path | None = None,
    claim_identities: frozenset[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    if depth <= 0:
        return []

    seen = visited if visited is not None else set()
    active_run_root = run_root or _recognized_output_run_root(base)
    active_claim_identities = claim_identities
    if active_run_root is not None and active_claim_identities is None:
        active_claim_identities = collect_output_claim_identities(active_run_root)
    try:
        entries = list(base.iterdir())
    except PermissionError as exc:
        raise HTTPException(403, detail="permission denied") from exc
    except OSError as exc:
        raise HTTPException(400, detail=f"cannot read directory: {exc}") from exc

    resolved_entries: list[tuple[str, Path, bool, bool]] = []
    for entry in entries:
        if entry.name.startswith(".") and not entry.name.startswith(".env"):
            continue
        try:
            is_dir = entry.is_dir()
            is_file = entry.is_file()
        except OSError:
            continue
        if is_dir or is_file:
            resolved_entries.append((entry.name, entry, is_dir, is_file))

    resolved_entries.sort(key=lambda item: (not item[2], item[0].lower()))

    nodes: list[dict[str, Any]] = []
    for name, entry, is_dir, is_file in resolved_entries:
        if is_dir:
            if name in _FILE_TREE_IGNORED_DIRS:
                continue
            entry_run_root = active_run_root or _recognized_output_run_root(entry)
            if entry_run_root is not None:
                if entry.is_symlink() or not is_contained_output_path(
                    entry,
                    output_root=entry_run_root,
                ):
                    continue
            try:
                real = entry.resolve(strict=True)
            except OSError:
                continue
            if real in seen:
                continue
            next_seen = set(seen)
            next_seen.add(real)
            nodes.append(
                {
                    "name": name,
                    "path": str(entry),
                    "type": "directory",
                    "children": _scan_file_tree(
                        entry,
                        depth - 1,
                        next_seen,
                        run_root=entry_run_root,
                        claim_identities=(
                            active_claim_identities
                            if entry_run_root == active_run_root
                            else None
                        ),
                    ),
                }
            )
        elif is_file:
            try:
                entry_run_root = active_run_root or _recognized_output_run_root(entry)
                entry_claim_identities = active_claim_identities
                if entry_run_root is not None and entry_claim_identities is None:
                    entry_claim_identities = collect_output_claim_identities(
                        entry_run_root
                    )
                if entry_run_root is not None and not is_scientific_output_file(
                    entry,
                    output_root=entry_run_root,
                    claim_identities=entry_claim_identities,
                ):
                    continue
                nodes.append(_classify_tree_file(entry))
            except OSError:
                continue
    return nodes


@app.get("/files/tree", dependencies=[Depends(require_bearer_token)])
async def files_tree(
    path: str = Query(..., description="Directory path on the backend host"),
    depth: int = Query(3, ge=1, le=10),
):
    """Return files and directories under a backend-host directory."""
    raw = str(path or "").strip()
    if not raw:
        raise HTTPException(400, detail="path is required")
    candidate = Path(raw).expanduser()
    try:
        base = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="directory does not exist") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, detail=f"invalid path: {exc}") from exc
    if not base.is_dir():
        raise HTTPException(400, detail="path is not a directory")
    run_root = _recognized_output_run_root(candidate)
    if run_root is not None and not is_contained_output_path(
        candidate,
        output_root=run_root,
    ):
        raise HTTPException(404, detail="directory does not exist")

    return {
        "root": str(base),
        "tree": _scan_file_tree(base, depth, run_root=run_root),
    }


@app.get("/files/serve", dependencies=[Depends(require_bearer_token)])
async def files_serve(
    path: str = Query(..., description="Trusted file path on the backend host"),
):
    """Serve a file from the backend host under trusted workspace/output roots."""
    target = _resolve_trusted_file_path(path)
    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        str(target),
        media_type=media_type or "application/octet-stream",
        filename=target.name,
        headers={"Cache-Control": "private, max-age=60"},
    )


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


async def health():
    core = _get_core()
    payload = {
        "status": "ok",
        "version": __version__,
        "backend_process_epoch": _BACKEND_PROCESS_EPOCH,
        "provider": core.LLM_PROVIDER_NAME,
        "model": core.OMICSCLAW_MODEL,
        **_skills_count_payload(core),
        "kg": _kg_status_payload(),
        "contracts": {
            "desktop_chat": desktop_chat_contract(),
            "desktop_run": desktop_run_contract(),
            "desktop_turn_observation": desktop_turn_observation_contract(),
            "desktop_turn_submission": desktop_turn_submission_contract(),
        },
        **_runtime_health_payload(core),
    }
    # Production serves full health only after lifespan has bound the exclusive
    # Control Runtime. Test-only direct function calls without lifespan retain
    # their historical diagnostic payload, while every live authenticated
    # Backend exposes the immutable Database authority identity.
    if _desktop_control_runtime is not None:
        payload["control_authority_id"] = (
            _desktop_control_runtime.repository.control_authority_id
        )
    return payload


@app.api_route("/health", methods=["GET", "HEAD"], name="health")
async def _health_endpoint(request: Request):
    authority = remote_bearer_authority_for_app(request.app)
    if authority.configured:
        authorization_values = request.headers.getlist("authorization")
        if not authorization_values:
            return {
                "status": "ok",
                "version": __version__,
                "launch_id": str(os.getenv("OMICSCLAW_DESKTOP_LAUNCH_ID", "") or ""),
                "auth_required": True,
            }
        authority.enforce(
            authorization_values[0] if len(authorization_values) == 1 else ""
        )
    return await health()


# ---------------------------------------------------------------------------
# Skill detail helpers — read the richer per-skill metadata that lives on disk
# (the narrative SKILL.md body, references/) plus the dual-track contract
# (skill.yaml v2 / SKILL.md frontmatter + parameters.yaml v1) that is not part
# of the in-memory registry info dict.
# ---------------------------------------------------------------------------


def _skill_metadata(skill_dir: Path | None):
    """Dual-track per-skill metadata, or ``None`` for a missing directory.

    Reads version/author/license/tags/requires through ``LazySkillMetadata`` —
    the SAME dual-track reader the registry and catalog use (ADR 0037) — so a
    skill reports identical facts whether it ships a v2 ``skill.yaml`` or a v1
    ``SKILL.md`` frontmatter + ``parameters.yaml``. Properties are read lazily,
    so constructing this is cheap.
    """
    if skill_dir is None:
        return None
    from omicsclaw.skill.lazy_metadata import LazySkillMetadata

    return LazySkillMetadata(skill_dir)


def _skill_security_status(meta) -> dict[str, object]:
    """Expose reviewed declaration status without implying OS confinement."""
    from omicsclaw.skill.execution_contract import describe_skill_security

    return describe_skill_security(
        {
            "security_contract": meta.security_contract if meta else {},
            "security_reviewed": meta.security_reviewed if meta else False,
        }
    ).to_dict()


def _skill_source(script_path: Path | None) -> str:
    """builtin if the skill ships under the repo SKILLS_DIR, else user-installed."""
    if script_path is None:
        return "user"
    try:
        from omicsclaw.skill.registry import SKILLS_DIR

        script_path.resolve().relative_to(SKILLS_DIR.resolve())
        return "builtin"
    except Exception:
        return "user"


def _skill_health(status: str, *, skill_md_exists: bool, script_exists: bool) -> str:
    if status != "ready" or not script_exists or not skill_md_exists:
        return "degraded"
    return "healthy"


def _skill_resources(skill_dir: Path | None, script_path: Path | None) -> list[dict]:
    if skill_dir is None:
        return []
    resources: list[dict] = []
    if script_path is not None and script_path.exists():
        resources.append({"path": script_path.name, "kind": "script"})
    # skill.yaml (v2 contract) and parameters.yaml (v1 sidecar) are both config;
    # a skill ships one or the other (or both, mid-migration) — list whatever exists.
    for special, kind in (
        ("SKILL.md", "doc"),
        ("skill.yaml", "config"),
        ("parameters.yaml", "config"),
    ):
        if (skill_dir / special).exists():
            resources.append({"path": special, "kind": kind})
    ref_dir = skill_dir / "references"
    if ref_dir.is_dir():
        for f in sorted(ref_dir.glob("*.md")):
            resources.append({"path": f"references/{f.name}", "kind": "reference"})
    script_name = script_path.name if script_path is not None else None
    for py in sorted(skill_dir.glob("*.py")):
        if py.name != script_name:
            resources.append({"path": py.name, "kind": "script"})
    return resources


def _skill_references(skill_dir: Path | None) -> list[dict]:
    if skill_dir is None:
        return []
    ref_dir = skill_dir / "references"
    if not ref_dir.is_dir():
        return []
    refs: list[dict] = []
    for f in sorted(ref_dir.glob("*.md")):
        title = f.stem.replace("_", " ").replace("-", " ").title()
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    title = stripped.lstrip("#").strip() or title
                    break
        except OSError:
            pass
        refs.append({"title": title, "path": f"references/{f.name}"})
    return refs


def _skill_diagnostics(
    skill_dir: Path | None, script_path: Path | None, version: str | None
) -> list[dict]:
    if skill_dir is None:
        return []
    checks: list[dict] = []

    def add(label: str, ok: bool, detail: str, *, warn_only: bool = False) -> None:
        status = "pass" if ok else ("warn" if warn_only else "fail")
        checks.append({"label": label, "status": status, "detail": detail})

    skill_md_exists = (skill_dir / "SKILL.md").exists()
    add(
        "SKILL.md",
        skill_md_exists,
        "skill definition found" if skill_md_exists else "SKILL.md missing",
    )
    script_exists = script_path is not None and script_path.exists()
    add(
        "entry script",
        script_exists,
        (script_path.name if script_exists and script_path else "entry script missing"),
    )
    has_version = bool(version)
    add(
        "version declared",
        has_version,
        f"version {version}" if has_version else "no version declared",
        warn_only=True,
    )
    # Runtime contract: v2 skill.yaml or v1 parameters.yaml (ADR 0037 dual-track).
    skill_yaml_exists = (skill_dir / "skill.yaml").exists()
    params_exists = (skill_dir / "parameters.yaml").exists()
    if skill_yaml_exists:
        contract_label, contract_detail = "skill.yaml", "runtime contract declared (v2)"
    else:
        contract_label = "parameters.yaml"
        contract_detail = (
            "runtime contract declared" if params_exists else "no parameters.yaml"
        )
    add(
        contract_label,
        skill_yaml_exists or params_exists,
        contract_detail,
        warn_only=True,
    )
    has_refs = (skill_dir / "references").is_dir()
    add(
        "references",
        has_refs,
        "references/ present" if has_refs else "no references/ directory",
        warn_only=True,
    )
    return checks


# ---------------------------------------------------------------------------
# GET /skills — list all skills grouped by domain
# ---------------------------------------------------------------------------


@app.get("/skills")
async def list_skills():
    core = _get_core()
    skill_registry = core._skill_registry()

    domain_groups: dict[str, list[dict]] = {}

    for alias, info in skill_registry.skills.items():
        # Skip alias pointers (only show canonical entries)
        if alias != info.get("alias", alias):
            continue

        domain = info.get("domain", "other")
        script = info.get("script")
        script_path = Path(script) if script else None
        skill_dir = script_path.parent if script_path else None
        status = "ready" if (script_path and script_path.exists()) else "planned"
        meta = _skill_metadata(skill_dir)
        version = meta.version if meta else None
        readiness = _skill_health(
            status,
            skill_md_exists=bool(skill_dir and (skill_dir / "SKILL.md").exists()),
            script_exists=bool(script_path and script_path.exists()),
        )
        entry = {
            "name": alias,
            "description": info.get("description", ""),
            "domain": domain,
            "status": status,
            # Governance lifecycle (draft/mvp/stable/deprecated) + authorship —
            # distinct from `status` above, which is script-on-disk availability.
            "lifecycle_status": meta.lifecycle_status if meta else "mvp",
            "superseded_by": (meta.superseded_by if meta else "") or None,
            "validation_level": meta.validation_level if meta else "smoke-only",
            "origin": meta.origin if meta else "human",
            "security": _skill_security_status(meta),
            "version": str(version) if version else None,
            # ``health`` is retained as a compatibility alias.  The value is
            # file readiness, not the evidence ledger's runtime health.
            "readiness": readiness,
            "health": readiness,
        }
        domain_groups.setdefault(domain, []).append(entry)

    # Build response with domain metadata
    result = []
    for domain_key, domain_info in skill_registry.domains.items():
        skills_in_domain = domain_groups.get(domain_key, [])
        if not skills_in_domain:
            continue
        result.append(
            {
                "domain": domain_key,
                "domain_name": domain_info.get("name", domain_key.title()),
                "primary_data_types": domain_info.get("primary_data_types", []),
                "skills": skills_in_domain,
            }
        )

    # Include "other" domain for dynamically discovered skills
    known_domains = set(skill_registry.domains.keys())
    other_skills = []
    for domain_key, skills in domain_groups.items():
        if domain_key not in known_domains:
            other_skills.extend(skills)
    if other_skills:
        result.append(
            {
                "domain": "other",
                "domain_name": "Other (Dynamically Discovered)",
                "primary_data_types": [],
                "skills": other_skills,
            }
        )

    return {"domains": result, "total": sum(len(d["skills"]) for d in result)}


# ---------------------------------------------------------------------------
# GET /skills/{domain}/{skill_name} — single skill detail
# ---------------------------------------------------------------------------


@app.get("/skills/{domain}/{skill_name}")
async def get_skill(domain: str, skill_name: str):
    core = _get_core()
    skill_registry = core._skill_registry()

    info = skill_registry.skills.get(skill_name)
    if info is None:
        raise HTTPException(404, detail=f"Skill '{skill_name}' not found")

    skill_domain = info.get("domain", "other")
    if skill_domain != domain:
        raise HTTPException(
            404,
            detail=f"Skill '{skill_name}' belongs to domain '{skill_domain}', not '{domain}'",
        )

    script = info.get("script")
    script_path = Path(script) if script else None
    skill_dir = script_path.parent if script_path else None
    status = "ready" if (script_path and script_path.exists()) else "planned"

    meta = _skill_metadata(skill_dir)
    skill_md_text: str | None = None
    if skill_dir is not None and (skill_dir / "SKILL.md").exists():
        try:
            skill_md_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        except OSError:
            skill_md_text = None

    # Dual-track identity metadata (ADR 0037): sourced from skill.yaml (v2) or
    # SKILL.md frontmatter (v1) via the same reader the registry/catalog use.
    version = meta.version if meta else None
    readiness = _skill_health(
        status,
        skill_md_exists=bool(skill_dir and (skill_dir / "SKILL.md").exists()),
        script_exists=bool(script_path and script_path.exists()),
    )
    return {
        "name": skill_name,
        "domain": skill_domain,
        "description": info.get("description", ""),
        "status": status,
        # Governance lifecycle (draft/mvp/stable/deprecated) + authorship —
        # distinct from `status` above, which is script-on-disk availability.
        "lifecycle_status": meta.lifecycle_status if meta else "mvp",
        "superseded_by": (meta.superseded_by if meta else "") or None,
        "validation_level": meta.validation_level if meta else "smoke-only",
        "origin": meta.origin if meta else "human",
        "security": _skill_security_status(meta),
        "script_path": str(script) if script else None,
        "aliases": [
            a
            for a, i in skill_registry.skills.items()
            if i.get("alias") == skill_name and a != skill_name
        ],
        # --- richer detail (skill.yaml v2 / SKILL.md+parameters.yaml v1) ---
        "version": str(version) if version else None,
        "source": _skill_source(script_path),
        # Existence — NOT read-success — mirrors list_skills so the two
        # endpoints agree even when SKILL.md exists but is unreadable.
        "readiness": readiness,
        "health": readiness,
        "author": (meta.author if meta else "") or None,
        "license": (meta.license if meta else "") or None,
        "tags": [str(t) for t in (meta.tags if meta else []) if str(t).strip()],
        "requires": [str(r) for r in (meta.requires if meta else []) if str(r).strip()],
        "skill_md": skill_md_text,
        "resources": _skill_resources(skill_dir, script_path),
        "references": _skill_references(skill_dir),
        "diagnostics": _skill_diagnostics(skill_dir, script_path, version),
    }


# ---------------------------------------------------------------------------
# Skill evolution governance — evidence ledger + human-gated decisions
# ---------------------------------------------------------------------------


def _skill_evolution_governance():
    from omicsclaw.skill.evolution_governance import (
        default_skill_evolution_governance,
    )

    return default_skill_evolution_governance()


async def _require_skill_evolution_bearer_token(
    request: Request,
) -> None:
    """Fail closed for governance reads and mutations.

    Remote execution routes intentionally retain their local-first no-token
    default.  Skill evolution can mutate canonical repository state, so its
    dedicated boundary is unavailable until an explicit shared token exists.
    """

    authorization_values = request.headers.getlist("authorization")
    authorization: str | None = None
    if len(authorization_values) == 1:
        authorization = authorization_values[0]
    elif authorization_values:
        authorization = ""
    _skill_evolution_bearer_policy_for_scope(request.scope).enforce(authorization)


@app.get(
    "/skill-evolution",
    dependencies=[Depends(_require_skill_evolution_bearer_token)],
)
async def get_skill_evolution_snapshot():
    governance = _skill_evolution_governance()
    return await asyncio.to_thread(governance.snapshot)


@app.post(
    "/skill-evolution/refresh",
    dependencies=[Depends(_require_skill_evolution_bearer_token)],
)
async def refresh_skill_evolution():
    governance = _skill_evolution_governance()
    try:
        created = await asyncio.to_thread(governance.refresh)
        snapshot = await asyncio.to_thread(governance.snapshot)
    except Exception as exc:
        logger.exception("Skill evolution refresh failed")
        raise HTTPException(500, detail=str(exc)) from exc
    return {
        "created": [proposal.to_dict() for proposal in created],
        **snapshot,
    }


@app.post(
    "/skill-evolution/proposals/gotcha",
    dependencies=[Depends(_require_skill_evolution_bearer_token)],
)
async def propose_skill_gotcha(req: EvolutionGotchaProposalRequest):
    from omicsclaw.skill.evolution_governance import EvolutionRevalidationError

    governance = _skill_evolution_governance()
    try:
        proposal = await asyncio.to_thread(
            governance.propose_gotcha,
            target_skill=req.target_skill,
            proposer=req.proposer,
            reason=req.reason,
            support_event_ids=req.support_event_ids,
            entry=req.entry.model_dump(),
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (EvolutionRevalidationError, ValueError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Skill Gotcha proposal failed")
        raise HTTPException(500, detail=str(exc)) from exc
    return proposal.to_dict()


@app.post(
    "/skill-evolution/proposals/deprecation",
    dependencies=[Depends(_require_skill_evolution_bearer_token)],
)
async def propose_skill_deprecation(req: EvolutionDeprecationProposalRequest):
    from omicsclaw.skill.evolution_governance import EvolutionRevalidationError

    governance = _skill_evolution_governance()
    try:
        proposal = await asyncio.to_thread(
            governance.propose_deprecation,
            target_skill=req.target_skill,
            replacement_skill=req.replacement_skill,
            proposer=req.proposer,
            reason=req.reason,
            support_event_ids=req.support_event_ids,
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (EvolutionRevalidationError, ValueError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Skill deprecation proposal failed")
        raise HTTPException(500, detail=str(exc)) from exc
    return proposal.to_dict()


@app.post(
    "/skill-evolution/reconcile",
    dependencies=[Depends(_require_skill_evolution_bearer_token)],
)
async def reconcile_skill_evolution(req: EvolutionReconciliationRequest):
    from omicsclaw.skill.evolution_governance import EvolutionRevalidationError

    governance = _skill_evolution_governance()
    try:
        return await asyncio.to_thread(
            governance.reconcile,
            operator=req.operator,
            reason=req.reason,
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (EvolutionRevalidationError, ValueError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Skill evolution reconciliation failed")
        raise HTTPException(500, detail=str(exc)) from exc


@app.post(
    "/skill-evolution/{proposal_id}/approve",
    dependencies=[Depends(_require_skill_evolution_bearer_token)],
)
async def approve_skill_evolution(
    proposal_id: str,
    req: EvolutionApprovalRequest,
):
    from omicsclaw.skill.evolution_governance import EvolutionRevalidationError

    governance = _skill_evolution_governance()
    try:
        receipt = await asyncio.to_thread(
            governance.approve,
            proposal_id,
            approver=req.approver,
            reason=req.reason,
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (EvolutionRevalidationError, ValueError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Skill evolution approval failed")
        raise HTTPException(500, detail=str(exc)) from exc
    return {
        "proposal_id": receipt.proposal_id,
        "status": receipt.status,
        "approved_by": receipt.approved_by,
        "before_hash": receipt.before_hash,
        "after_hash": receipt.after_hash,
    }


@app.post(
    "/skill-evolution/{proposal_id}/reject",
    dependencies=[Depends(_require_skill_evolution_bearer_token)],
)
async def reject_skill_evolution(
    proposal_id: str,
    req: EvolutionRejectionRequest,
):
    from omicsclaw.skill.evolution_governance import EvolutionRevalidationError

    governance = _skill_evolution_governance()
    try:
        proposal = await asyncio.to_thread(
            governance.reject,
            proposal_id,
            approver=req.approver,
            reason=req.reason,
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except (EvolutionRevalidationError, ValueError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Skill evolution rejection failed")
        raise HTTPException(500, detail=str(exc)) from exc
    return proposal.to_dict()


# ---------------------------------------------------------------------------
# GET /skills/installed — list OmicsClaw user-installed skill packs
# ---------------------------------------------------------------------------


@app.get("/skills/installed")
async def list_installed_skills():
    try:
        from omicsclaw.extensions import list_installed_extensions

        entries = await asyncio.to_thread(
            list_installed_extensions,
            _omicsclaw_project_dir(),
            extension_types=("skill-pack",),
        )

        result: list[dict[str, Any]] = []
        for entry in entries:
            record = entry.record
            result.append(
                {
                    "name": record.extension_name
                    if record is not None
                    else entry.path.name,
                    "directory_name": entry.path.name,
                    "manifest_name": record.manifest_name if record is not None else "",
                    "source": record.source if record is not None else "",
                    "source_kind": record.source_kind if record is not None else "",
                    "installed_at": record.installed_at if record is not None else "",
                    "path": str(entry.path),
                    "relative_install_path": record.relative_install_path
                    if record is not None
                    else "",
                    "tracked": record is not None,
                    "enabled": entry.state.enabled,
                    "disabled_reason": entry.state.disabled_reason,
                }
            )

        result.sort(
            key=lambda item: ((item["installed_at"] or ""), item["name"].lower()),
            reverse=True,
        )
        return {"skills": result, "total": len(result)}
    except Exception as exc:
        logger.exception("Installed skills list failed")
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /skills/install — install a user skill pack into OmicsClaw
# ---------------------------------------------------------------------------


@app.post("/skills/install")
async def install_skill(req: SkillInstallRequest):
    source = req.source.strip()
    if not source:
        raise HTTPException(400, detail="source is required")

    try:
        from omicsclaw.surfaces.cli._skill_management_support import (
            install_skill_from_source,
        )

        statuses = await asyncio.to_thread(
            install_skill_from_source,
            source,
            omicsclaw_dir=_omicsclaw_project_dir(),
        )
    except Exception as exc:
        logger.exception("Skill install failed")
        raise HTTPException(500, detail=str(exc))

    success = not any(
        str(getattr(status, "level", "")) == "error" for status in statuses
    )
    return {
        "success": success,
        "source": source,
        "statuses": _serialize_skill_command_statuses(statuses),
    }


# ---------------------------------------------------------------------------
# POST /skills/uninstall — remove a user-installed skill pack
# ---------------------------------------------------------------------------


@app.post("/skills/uninstall")
async def uninstall_skill(req: SkillUninstallRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, detail="name is required")

    try:
        from omicsclaw.surfaces.cli._skill_management_support import uninstall_extension

        statuses = await asyncio.to_thread(
            uninstall_extension,
            name,
            omicsclaw_dir=_omicsclaw_project_dir(),
            expected_type="skill-pack",
        )
    except Exception as exc:
        logger.exception("Skill uninstall failed")
        raise HTTPException(500, detail=str(exc))

    success = not any(
        str(getattr(status, "level", "")) == "error" for status in statuses
    )
    return {
        "success": success,
        "name": name,
        "statuses": _serialize_skill_command_statuses(statuses),
    }


# ---------------------------------------------------------------------------
# GET /memory/browse — browse memory graph nodes
# ---------------------------------------------------------------------------


async def _decorate_analysis_titles(children: list) -> None:
    """Replace ``name`` with a derived display label for analysis://*
    children, in-place. See
    docs/adr/0002-derived-display-label-for-analysis-memory.md.

    The engine's ``list_children_rich`` returns a 100-char
    ``content_snippet`` that's too short to carry the full
    ``executed_at``/``parameters.input``, so we fetch the full content
    via ``recall`` for each analysis child. Cost = N indexed reads where
    N = number of analysis children rendered in the current tree level
    (typically ≤ 50 in the desktop UI). Silent fallback to ``edge.name``
    on parse failure or missing fields means the UI never renders blank.
    """
    if _memory_client is None:
        return
    from omicsclaw.memory.compat import _analysis_content_to_title

    for child in children:
        if child.get("domain") != "analysis":
            continue
        path = child.get("path")
        if not isinstance(path, str) or not path:
            continue
        try:
            record = await _memory_client.recall(f"analysis://{path}")
        except Exception:
            continue
        if record is None or not getattr(record, "content", None):
            continue
        title = _analysis_content_to_title(record.content)
        if title is not None:
            child["name"] = title


@app.get("/memory/browse")
async def memory_browse(
    path: str = Query("", description="Node path to browse"),
    domain: str = Query("core", description="Memory domain"),
):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        from dataclasses import asdict

        from omicsclaw.memory.namespace_policy import should_version
        from omicsclaw.memory.uri import MemoryURI

        uri = f"{domain}://{path}" if path else f"{domain}://"
        # list_children_rich returns the desktop-tree dict shape
        # (name, path, domain, content_snippet, approx_children_count, …)
        # the UI's MemoryTree/MemoryContent rely on for labels and the
        # directory-vs-leaf icon. include_shared=True so __shared__
        # children (e.g., core://agent/*) appear alongside the user's own.
        children = await _memory_client.list_children_rich(
            uri,
            context_domain=domain,
            context_path=path,
            include_shared=True,
        )
        await _decorate_analysis_titles(children)

        # Also get the node itself if path is provided
        node = None
        if path:
            record = await _memory_client.recall(f"{domain}://{path}")
            if record is not None:
                node = asdict(record)

        # is_versioned tells the desktop UI whether the URI has a
        # version chain (and therefore whether to surface the
        # History/rollback button). Mirrors the namespace_policy rule
        # the engine uses to pick upsert_versioned vs upsert.
        is_versioned = should_version(MemoryURI.parse(uri))

        return {
            "path": path,
            "domain": domain,
            "node": node,
            "children": children,
            "is_versioned": is_versioned,
        }
    except Exception as exc:
        logger.exception("Memory browse error")
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /memory/search — full-text search across memories
# ---------------------------------------------------------------------------


@app.get("/memory/search")
async def memory_search(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=100, description="Max results"),
    domain: Optional[str] = Query(None, description="Optional domain filter"),
):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        results = await _memory_client.search(q, limit=limit, domain=domain)
        return {"query": q, "results": results, "count": len(results)}
    except Exception as exc:
        logger.exception("Memory search error")
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# Thread (Bench investigation) CRUD — BE-THREAD-CRUD-2 (Phase 1)
# Authoritative metadata at project://<thread_id> (ThreadMemory, versioned).
# All operations run in the desktop _memory_client namespace; the URL
# thread_id is only a node-path lookup key, never trusted as a namespace.
# NOTE: static routes (/thread/create, /thread/list) are declared BEFORE the
# dynamic /thread/{thread_id} so the literal paths are not captured by it.
# ---------------------------------------------------------------------------


class ThreadCreateRequest(BaseModel):
    name: str
    description: str = ""
    domains: list[str] = Field(default_factory=list)
    organism: Optional[str] = None
    platforms: list[str] = Field(default_factory=list)
    venue: Optional[str] = None


class ThreadUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    domains: Optional[list[str]] = None
    organism: Optional[str] = None
    platforms: Optional[list[str]] = None
    venue: Optional[str] = None


class ThreadPreferenceRequest(BaseModel):
    key: str
    value: Any


class ThreadFormalizeRequest(BaseModel):
    hunch: str
    stub: bool = False  # use the offline LLM stub (tests / dry-runs)


class ThreadConfirmVerdictRequest(BaseModel):
    verdict: str  # validated | refuted | refined | inconclusive (ADR 0021 §6)


class ThreadRoutePreviewRequest(BaseModel):
    claim: str  # the hypothesis claim to route (ADR 0021 §4)


def _seed_project_dir(thread_id: str, name: str) -> None:
    """ADR 0035: pre-create the readable on-disk Project directory for a Bench
    thread so its runs land under ``<name-slug>__<short-id>/`` (not an opaque
    ``project__<hash>`` built when the agent runs without the thread name), and so
    a thread rename keeps ``project_meta.json``'s display name in sync. Best-effort
    — never let filesystem seeding break thread CRUD."""
    try:
        from omicsclaw.common import run_paths

        core = _get_core()
        run_paths.resolve_project_dir(
            Path(str(core.OUTPUT_DIR)), thread_id, name or "", create=True
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("project dir seed skipped for thread %s", thread_id, exc_info=True)


@app.post("/thread/create")
async def thread_create(req: ThreadCreateRequest):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import thread as thread_svc

        tm = await thread_svc.create_thread(
            _memory_client,
            name=req.name,
            description=req.description,
            domains=req.domains,
            organism=req.organism,
            platforms=req.platforms,
            venue=req.venue,
        )
        _seed_project_dir(tm.thread_id, tm.name)
        return tm.model_dump()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Thread create error")
        raise HTTPException(500, detail=str(exc))


@app.get("/thread/list")
async def thread_list():
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import thread as thread_svc

        threads = await thread_svc.list_threads(_memory_client)
        return {"threads": [t.model_dump() for t in threads], "count": len(threads)}
    except Exception as exc:
        logger.exception("Thread list error")
        raise HTTPException(500, detail=str(exc))


@app.get("/thread/{thread_id}")
async def thread_get(thread_id: str):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import thread as thread_svc

        tm = await thread_svc.get_thread(_memory_client, thread_id)
        if tm is None:
            raise HTTPException(404, detail=f"thread not found: {thread_id}")
        return tm.model_dump()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Thread get error")
        raise HTTPException(500, detail=str(exc))


async def _thread_source_slugs(thread_id: str) -> list[str] | None:
    """The thread's recorded KG Source slugs, or None when per-thread scoping is
    unavailable (memory off) → callers fall back to workspace-wide (批7).

    The memory recall is async; the desktop _memory_client reads the same
    namespace the in-loop ingest wrote to (see ``orchestration._capture_thread_source``).
    """
    if _memory_client is None:
        return None
    from omicsclaw.surfaces.desktop import thread as thread_svc

    return await thread_svc.list_thread_source_slugs(_memory_client, thread_id)


# The Ideate endpoints below are `async def` (批7): they first `await` the
# per-thread Source slugs from the (async) memory client, then offload the
# BLOCKING KG file IO / LLM round-trip to a worker thread via asyncio.to_thread
# so the event loop (and SSE chat streaming) is never blocked.
@app.get("/thread/{thread_id}/hypotheses")
async def thread_hypotheses(thread_id: str):
    """List Bench Ideate hypotheses for a thread (ADR 0021).

    The listing stays workspace-wide (a thread can see hypotheses formed
    elsewhere); 批7 adds the per-thread cross-study (跨课题) badge by passing the
    thread's Source slugs. Soft-fails to an empty list when KG is unavailable so
    the Ideate panel degrades gracefully.
    """
    if not _KG_AVAILABLE:
        return {"thread_id": thread_id, "hypotheses": [], "kg_available": False}
    home = _resolve_shared_kg_home()
    if not home:
        return {"thread_id": thread_id, "hypotheses": [], "kg_available": False}
    from omicsclaw.surfaces.desktop import hypotheses as hyp_svc

    slugs = await _thread_source_slugs(thread_id)
    thread_slugs = set(slugs) if slugs is not None else None
    try:
        items = await asyncio.to_thread(
            hyp_svc.list_workspace_hypotheses, home, thread_slugs=thread_slugs
        )
    except Exception as exc:
        logger.exception("Hypothesis listing error")
        raise HTTPException(500, detail=str(exc))
    return {"thread_id": thread_id, "hypotheses": items, "kg_available": True}


@app.get("/thread/{thread_id}/sources")
async def thread_sources(thread_id: str):
    """List the KG Sources ingested in this thread (批7, per-thread grounding).

    Reads the thread's recorded Source slugs from memory and enriches them with
    title/state from the workspace KG (stale/deleted slugs dropped). Soft-fails to
    an empty list when KG/memory is unavailable so the Read panel degrades
    gracefully.
    """
    if not _KG_AVAILABLE:
        return {
            "thread_id": thread_id,
            "sources": [],
            "returned": 0,
            "total": 0,
            "kg_available": False,
        }
    home = _resolve_shared_kg_home()
    if not home:
        return {
            "thread_id": thread_id,
            "sources": [],
            "returned": 0,
            "total": 0,
            "kg_available": False,
        }
    from omicsclaw.surfaces.desktop import hypotheses as hyp_svc

    slugs = await _thread_source_slugs(thread_id) or []
    try:
        sources = await asyncio.to_thread(hyp_svc.list_thread_sources, home, slugs)
    except Exception as exc:
        logger.exception("Thread sources error")
        raise HTTPException(500, detail=str(exc))
    return {
        "thread_id": thread_id,
        "sources": sources,
        "returned": len(sources),
        "total": len(sources),
        "kg_available": True,
    }


@app.post("/thread/{thread_id}/formalize")
async def thread_formalize(thread_id: str, req: ThreadFormalizeRequest):
    """Formalize a free-text hunch into a thread-grounded hypothesis (ADR 0021 §2).

    批7: grounds against the THREAD's Sources (its per-thread citation allow-list)
    rather than every workspace Source, so ``cross_study`` is real and a thread
    only cites what it has read. Falls back to workspace-wide when memory is
    unavailable. This in-process route is the canonical desktop formalize path;
    the KG-native ``/kg/ideate/formalize`` (body-driven slugs) is for headless KG
    use only and the App never calls it.
    """
    if not _KG_AVAILABLE:
        raise HTTPException(503, detail="OmicsClaw-KG is not available")
    home = _resolve_shared_kg_home()
    if not home:
        raise HTTPException(400, detail="no workspace / KG home resolved")
    from omicsclaw.surfaces.desktop import hypotheses as hyp_svc
    from omicsclaw_kg.ideation.hypotheses import HypothesisIdeationError
    from omicsclaw_kg.llm.stub import StubLLMClient

    # A-1: ground KG ideation in the SAME provider that powers the desktop chat
    # (the live ``_core.llm``), via the OpenAI-compatible adapter, so formalize
    # works for any configured provider instead of hard-requiring ANTHROPIC_API_KEY.
    # `stub` selects the offline client for tests / dry-runs.
    if req.stub:
        llm = StubLLMClient()
    else:
        from omicsclaw.runtime.tools.kg_tools import _build_kg_extractor

        llm = _build_kg_extractor()
        if llm is None:
            raise HTTPException(
                503,
                detail="no LLM provider configured; set a provider in Settings (LLM_API_KEY) to formalize hypotheses",
            )
    slugs = await _thread_source_slugs(thread_id)  # None → workspace-wide fallback
    try:
        hypothesis = await asyncio.to_thread(
            hyp_svc.formalize_thread_hypothesis, home, req.hunch, llm, slugs
        )
    except HypothesisIdeationError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("Formalize error")
        raise HTTPException(500, detail=str(exc))
    return {"thread_id": thread_id, "hypothesis": hypothesis}


@app.post("/thread/{thread_id}/hypothesis/{slug}/confirm-verdict")
def thread_confirm_verdict(thread_id: str, slug: str, req: ThreadConfirmVerdictRequest):
    """Confirm a hypothesis's suggested verdict (ADR 0021 §6): flip its status and
    clear the suggestion. Sync def → threadpooled KG IO (see thread_hypotheses)."""
    if not _KG_AVAILABLE:
        raise HTTPException(503, detail="OmicsClaw-KG is not available")
    home = _resolve_shared_kg_home()
    if not home:
        raise HTTPException(400, detail="no workspace / KG home resolved")
    from omicsclaw.surfaces.desktop import hypotheses as hyp_svc

    try:
        result = hyp_svc.confirm_thread_hypothesis_verdict(home, slug, req.verdict)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except Exception as exc:
        logger.exception("Confirm verdict error")
        raise HTTPException(500, detail=str(exc))
    return {"thread_id": thread_id, **result}


@app.post("/thread/{thread_id}/hypothesis/{slug}/route-preview")
async def thread_route_preview(
    thread_id: str, slug: str, req: ThreadRoutePreviewRequest
):
    """Preview the Analysis Router's recommendation for testing a hypothesis (ADR 0021 §4).

    Shows the recommended skill + route kind + the thread's bound dataset before the user
    commits to Analyze. Soft-fails to 503 when the memory system is unavailable.
    """
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    from omicsclaw.surfaces.desktop import hypotheses as hyp_svc

    try:
        return await hyp_svc.route_preview(_memory_client, thread_id, slug, req.claim)
    except Exception as exc:
        logger.exception("Route preview error")
        raise HTTPException(500, detail=str(exc))


class RunPacketRequest(BaseModel):
    packet_id: str = Field(
        ...,
        min_length=1,
        description="Outbox packet id (from kg_build_packet or experiment submit).",
    )
    input_path: str | None = Field(
        None,
        description="Explicit dataset path; defaults to the thread's bound dataset.",
    )
    verdict: str = Field(
        "inconclusive",
        description="validated | refuted | inconclusive. Defaults to inconclusive (analysis ran; the user confirms the scientific verdict in Ideate).",
    )


@app.get("/thread/{thread_id}/outbox")
def thread_outbox(thread_id: str):
    """List pending KG handoff packets the deterministic executor can run for this
    workspace (audit E). Soft-fails to 503 when OmicsClaw-KG is unavailable."""
    if not _KG_AVAILABLE:
        raise HTTPException(503, detail="OmicsClaw-KG is not available")
    home = _resolve_shared_kg_home()
    if not home:
        raise HTTPException(400, detail="no workspace / KG home resolved")
    from omicsclaw_kg import config as kg_config

    from omicsclaw.surfaces.desktop import handoff_executor as outbox_svc

    cfg = kg_config.resolve(home)
    return {"thread_id": thread_id, "packets": outbox_svc.list_outbox_packets(cfg)}


@app.post("/thread/{thread_id}/run-packet")
async def thread_run_packet(thread_id: str, req: RunPacketRequest):
    """Run an outbox packet to completion and record the result back to the KG
    (audit E: deterministic idea→analysis→verdict closure).

    Replaces relying on the LLM to voluntarily call ``kg_record_result``: given a
    packet, the server resolves the target skill + the thread's dataset, runs the
    skill, and records a ``HandoffResult`` (archiving the packet, updating the
    graph, advancing any experiment step).
    """
    if not _KG_AVAILABLE:
        raise HTTPException(503, detail="OmicsClaw-KG is not available")
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    home = _resolve_shared_kg_home()
    if not home:
        raise HTTPException(400, detail="no workspace / KG home resolved")
    from omicsclaw_kg import config as kg_config

    from omicsclaw.surfaces.desktop import handoff_executor as outbox_svc

    cfg = kg_config.resolve(home)
    try:
        result = await outbox_svc.run_packet(
            _memory_client,
            cfg,
            thread_id,
            req.packet_id,
            input_path=req.input_path or None,
            verdict=req.verdict,
        )
    except Exception as exc:
        logger.exception("run-packet error")
        raise HTTPException(500, detail=str(exc))
    if result.get("status") == "error":
        raise HTTPException(400, detail=result.get("error", "could not run packet"))
    return {"thread_id": thread_id, **result}


class RunHypothesisRequest(BaseModel):
    hypothesis_slug: str = Field(
        ..., min_length=1, description="Hypothesis page slug to test."
    )
    input_path: str | None = Field(
        None,
        description="Explicit dataset path; defaults to the thread's bound dataset.",
    )
    verdict: str = Field(
        "inconclusive",
        description="validated | refuted | inconclusive (the user confirms the scientific verdict in Ideate).",
    )


@app.post("/thread/{thread_id}/run-hypothesis")
async def thread_run_hypothesis(thread_id: str, req: RunHypothesisRequest):
    """One-click Ideate→Analyze (audit E-(2→3)): build a handoff packet for a
    hypothesis (Analysis-Router-authoritative skill — the SAME (claim, dataset)
    the route-preview showed) and run it via the deterministic outbox executor, so
    the result lands as a thread analysis (visible in the Analyze panel) WITHOUT
    the user manually composing a chat message.

    Augments — does NOT replace — the chat-in-the-loop path (ADR 0023 §6 keeps the
    composer pre-fill); this is the programmatic shortcut the audit asked for.
    Chains the existing build_packet (Router skill) + outbox.run_packet machinery.
    """
    if not _KG_AVAILABLE:
        raise HTTPException(503, detail="OmicsClaw-KG is not available")
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    home = _resolve_shared_kg_home()
    if not home:
        raise HTTPException(400, detail="no workspace / KG home resolved")
    from omicsclaw.memory.compat import resolve_thread_dataset_path
    from omicsclaw.runtime.tools import kg_tools
    from omicsclaw.surfaces.desktop import handoff_executor as outbox_svc

    slug = req.hypothesis_slug.strip()
    # Security: slug is attacker-influenced at the HTTP boundary and flows into
    # wiki/hypotheses/<slug>.md + the packet id + handoff/outbox/<packet_id>.json.
    # Gate it the same way the agent-tool path does before any KG filesystem write.
    if not kg_tools._is_safe_slug(slug):
        raise HTTPException(400, detail=f"invalid hypothesis_slug {slug!r}")
    kg = kg_tools._import_kg_handoff()  # soft-import: config/build_packet/write_packet
    if kg is None:
        raise HTTPException(503, detail="OmicsClaw-KG handoff is not available")
    cfg = kg.config.resolve(home)
    try:
        # Router-authoritative skill on the SAME (claim, thread dataset) the
        # Ideate route-preview used (D-3) — then build + run the packet.
        dataset_path = await resolve_thread_dataset_path(_memory_client, thread_id)
        target_skill = await asyncio.to_thread(
            kg_tools._router_skill_for_hypothesis, slug, home, dataset_path
        )
        packet = await asyncio.to_thread(kg.build_packet, cfg, slug, target_skill, None)
        await asyncio.to_thread(kg.write_packet, cfg, packet)
        result = await outbox_svc.run_packet(
            _memory_client,
            cfg,
            thread_id,
            packet.packet_id,
            input_path=req.input_path or None,
            verdict=req.verdict,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("run-hypothesis error")
        raise HTTPException(500, detail=str(exc))
    if result.get("status") == "error":
        raise HTTPException(400, detail=result.get("error", "could not run hypothesis"))
    return {
        "thread_id": thread_id,
        "hypothesis_slug": slug,
        "packet_id": packet.packet_id,
        **result,
    }


@app.put("/thread/{thread_id}")
async def thread_update(thread_id: str, req: ThreadUpdateRequest):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import thread as thread_svc

        tm = await thread_svc.update_thread(
            _memory_client, thread_id, req.model_dump(exclude_none=True)
        )
        if tm is None:
            raise HTTPException(404, detail=f"thread not found: {thread_id}")
        _seed_project_dir(
            tm.thread_id, tm.name
        )  # ADR 0035: keep project_meta name in sync
        return tm.model_dump()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Thread update error")
        raise HTTPException(500, detail=str(exc))


@app.delete("/thread/{thread_id}")
async def thread_delete(thread_id: str):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import thread as thread_svc

        ok = await thread_svc.delete_thread(_memory_client, thread_id)
        if not ok:
            raise HTTPException(404, detail=f"thread not found: {thread_id}")
        return {"ok": True, "thread_id": thread_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Thread delete error")
        raise HTTPException(500, detail=str(exc))


@app.get("/thread/{thread_id}/preference")
async def thread_get_preference(thread_id: str):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import thread as thread_svc

        prefs = await thread_svc.get_thread_preferences(_memory_client, thread_id)
        if prefs is None:
            raise HTTPException(404, detail=f"thread not found: {thread_id}")
        return {"thread_id": thread_id, "preferences": prefs}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Thread preference get error")
        raise HTTPException(500, detail=str(exc))


@app.put("/thread/{thread_id}/preference")
async def thread_set_preference(thread_id: str, req: ThreadPreferenceRequest):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import thread as thread_svc

        tm = await thread_svc.set_thread_preference(
            _memory_client, thread_id, req.key, req.value
        )
        if tm is None:
            raise HTTPException(404, detail=f"thread not found: {thread_id}")
        return {"thread_id": thread_id, "preferences": tm.preferences}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Thread preference set error")
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# Onboarding & global bench preferences (Phase 5, BE-ONBOARD-8 / BE-PREF-7)
# ---------------------------------------------------------------------------


class OnboardUserRequest(BaseModel):
    # The frontend's (<=5) onboarding answers, stored verbatim to the versioned
    # core://my_user. Free-form so the question set can evolve without a schema bump.
    profile: dict[str, Any] = Field(default_factory=dict)


class BenchPreferenceRequest(BaseModel):
    key: str
    value: Any


@app.post("/onboard/user")
async def onboard_user_route(req: OnboardUserRequest):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import onboarding

        return await onboarding.onboard_user(_memory_client, req.profile)
    except Exception as exc:
        logger.exception("Onboarding (user) error")
        raise HTTPException(500, detail=str(exc))


@app.post("/onboard/skip")
async def onboard_skip_route():
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import onboarding

        return await onboarding.skip_onboarding(_memory_client)
    except Exception as exc:
        logger.exception("Onboarding (skip) error")
        raise HTTPException(500, detail=str(exc))


@app.get("/onboard/status")
async def onboard_status_route():
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import onboarding

        return await onboarding.onboarding_status(_memory_client)
    except Exception as exc:
        logger.exception("Onboarding (status) error")
        raise HTTPException(500, detail=str(exc))


@app.put("/preference/bench")
async def set_bench_preference_route(req: BenchPreferenceRequest):
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")
    try:
        from omicsclaw.surfaces.desktop import onboarding

        return await onboarding.set_bench_preference(_memory_client, req.key, req.value)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("Bench preference set error")
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# Memory CRUD & Management Endpoints
# ---------------------------------------------------------------------------

# -- Pydantic models for memory endpoints -----------------------------------


class MemoryCreateRequest(BaseModel):
    parent_path: str = ""
    content: str
    title: str = ""
    domain: str = "core"
    priority: int = 0


class MemoryUpdateRequest(BaseModel):
    path: str
    domain: str = "core"
    content: Optional[str] = None
    priority: Optional[int] = None


class MemoryRollbackRequest(BaseModel):
    path: str = ""
    domain: str = "core"
    target_memory_id: int


class GlossaryRequest(BaseModel):
    keyword: str
    node_uuid: str


class ScopedPruneRequest(BaseModel):
    scope: str = ""
    apply: bool = False
    workspace: str = ""


# -- Helper: get graph / glossary / changeset services ----------------------


def _get_review_log():
    """Return the singleton ReviewLog (cold-path operations).

    Used by /memory/review/* endpoints. Falls back with a 503 if the
    memory module isn't installed or is mid-initialization.
    """
    try:
        from omicsclaw.memory import get_review_log
    except Exception as exc:
        raise HTTPException(503, detail=f"Memory system unavailable: {exc}")
    try:
        return get_review_log()
    except Exception as exc:
        raise HTTPException(503, detail=f"Memory system unavailable: {exc}")


def _get_glossary_service():
    """Return GlossaryService, raising 503 if the memory module is not available."""
    try:
        from omicsclaw.memory import get_glossary_service

        return get_glossary_service()
    except Exception as exc:
        raise HTTPException(503, detail=f"Memory glossary service unavailable: {exc}")


def _get_changeset_store():
    """Return ChangesetStore, raising 503 if the memory module is not available."""
    try:
        from omicsclaw.memory.snapshot import get_changeset_store

        return get_changeset_store()
    except Exception as exc:
        raise HTTPException(503, detail=f"Memory changeset store unavailable: {exc}")


def _memory_review_model_map():
    from omicsclaw.memory import Edge, GlossaryKeyword, Memory, Node, Path

    return {
        "nodes": Node,
        "memories": Memory,
        "edges": Edge,
        "paths": Path,
        "glossary_keywords": GlossaryKeyword,
    }


def _coerce_memory_snapshot_row(model_cls, row: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import DateTime as SADateTime

    payload: dict[str, Any] = {}
    for column in model_cls.__table__.columns:
        if column.name not in row:
            continue
        value = row[column.name]
        if isinstance(column.type, SADateTime) and isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                pass
        payload[column.name] = value
    return payload


def _memory_snapshot_row_identity(table: str, row: dict[str, Any]):
    if table == "nodes":
        return row["uuid"]
    if table in {"memories", "edges"}:
        return row["id"]
    if table == "paths":
        # PK is (namespace, domain, path) post-001_namespace migration. Older
        # snapshots predating the namespace column default to "__shared__".
        namespace = row.get("namespace") or "__shared__"
        return (namespace, row["domain"], row["path"])
    if table == "glossary_keywords":
        if "id" in row and row["id"] is not None:
            return row["id"]
        return None
    raise ValueError(f"Unsupported memory review table '{table}'")


async def _resolve_memory_review_node_uuid(
    *,
    table: str,
    ref: dict[str, Any] | None,
    all_rows: dict[str, Any],
    session,
) -> str:
    if not ref:
        return ""
    if table == "nodes":
        return str(ref.get("uuid", "") or "")
    if table in {"memories", "glossary_keywords"}:
        return str(ref.get("node_uuid", "") or "")
    if table == "edges":
        return str(ref.get("child_uuid", "") or "")
    if table == "paths":
        node_uuid = str(ref.get("node_uuid", "") or "")
        if node_uuid:
            return node_uuid
        edge_id = ref.get("edge_id")
        if edge_id is None:
            return ""

        edge_entry = all_rows.get(f"edges:{edge_id}")
        if isinstance(edge_entry, dict):
            edge_ref = edge_entry.get("after") or edge_entry.get("before")
            if isinstance(edge_ref, dict) and edge_ref.get("child_uuid"):
                return str(edge_ref["child_uuid"])

        from sqlalchemy import select

        model_map = _memory_review_model_map()
        edge_model = model_map["edges"]
        result = await session.execute(
            select(edge_model.child_uuid).where(edge_model.id == edge_id)
        )
        return str(result.scalar_one_or_none() or "")
    return ""


async def _discard_pending_memory_review_changes() -> dict[str, Any]:
    from sqlalchemy import select

    from omicsclaw.memory import get_db_manager, get_search_indexer
    from omicsclaw.memory.snapshot import _rows_equal

    store = _get_changeset_store()
    all_rows = store.get_all_rows_dict()
    changed_entries = [
        (key, entry)
        for key, entry in all_rows.items()
        if not _rows_equal(
            entry.get("table", ""), entry.get("before"), entry.get("after")
        )
    ]
    if not changed_entries:
        return {"discarded": 0, "remaining": 0, "nodes_refreshed": 0}

    model_map = _memory_review_model_map()
    delete_order = ("paths", "glossary_keywords", "edges", "memories", "nodes")
    restore_order = ("nodes", "memories", "edges", "paths", "glossary_keywords")
    affected_node_uuids: set[str] = set()
    by_table: dict[str, list[dict[str, Any]]] = {table: [] for table in model_map}
    for _, entry in changed_entries:
        table = str(entry.get("table", "") or "")
        if table in by_table:
            by_table[table].append(entry)

    db = get_db_manager()
    search = get_search_indexer()

    async with db.session() as session:
        for _, entry in changed_entries:
            node_uuid = await _resolve_memory_review_node_uuid(
                table=str(entry.get("table", "") or ""),
                ref=(entry.get("after") or entry.get("before")),
                all_rows=all_rows,
                session=session,
            )
            if node_uuid:
                affected_node_uuids.add(node_uuid)

        for table in delete_order:
            model_cls = model_map[table]
            for entry in by_table[table]:
                before = entry.get("before")
                after = entry.get("after")
                if before is not None or after is None:
                    continue

                identity = _memory_snapshot_row_identity(table, after)
                instance = None
                if identity is not None:
                    instance = await session.get(model_cls, identity)
                elif table == "glossary_keywords":
                    instance = (
                        await session.execute(
                            select(model_cls).where(
                                model_cls.keyword == after.get("keyword"),
                                model_cls.node_uuid == after.get("node_uuid"),
                            )
                        )
                    ).scalar_one_or_none()

                if instance is not None:
                    await session.delete(instance)

        for table in restore_order:
            model_cls = model_map[table]
            for entry in by_table[table]:
                before = entry.get("before")
                after = entry.get("after")
                if before is None or after is not None:
                    continue

                payload = _coerce_memory_snapshot_row(model_cls, before)
                if table == "memories":
                    identity = _memory_snapshot_row_identity(table, before)
                    if identity is None:
                        raise ValueError(
                            "Cannot restore memory review row without an identity"
                        )
                    existing = await session.get(model_cls, identity)
                    if existing is None and "content" not in payload:
                        raise ValueError(
                            "Cannot discard pending memory deletion because snapshot content is unavailable"
                        )
                await session.merge(model_cls(**payload))

        for table in restore_order:
            model_cls = model_map[table]
            for entry in by_table[table]:
                before = entry.get("before")
                after = entry.get("after")
                if before is None or after is None:
                    continue

                payload = _coerce_memory_snapshot_row(model_cls, before)
                await session.merge(model_cls(**payload))

        await session.flush()
        for node_uuid in sorted(affected_node_uuids):
            await search.refresh_search_documents_for_node(node_uuid, session=session)

    discarded = store.discard_all()
    return {
        "discarded": discarded,
        "remaining": store.get_change_count(),
        "nodes_refreshed": len(affected_node_uuids),
    }


# -- POST /memory/create ----------------------------------------------------


@app.post("/memory/create")
async def memory_create(req: MemoryCreateRequest):
    """Create a new memory node."""
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    if not req.content.strip():
        raise HTTPException(400, detail="Content must not be empty")

    try:
        # Build URI: domain://parent_path/title or domain://title
        if req.parent_path:
            path = f"{req.parent_path}/{req.title}" if req.title else req.parent_path
        else:
            path = req.title if req.title else ""
        uri = f"{req.domain}://{path}"

        result = await _memory_client.remember(
            uri=uri,
            content=req.content,
            priority=req.priority,
        )
        return {"ok": True, "uri": uri, "result": result}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("Memory create error")
        raise HTTPException(500, detail=str(exc))


# -- PUT /memory/update ------------------------------------------------------


@app.put("/memory/update")
async def memory_update(req: MemoryUpdateRequest):
    """Update an existing memory node's content or priority."""
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    if req.content is None and req.priority is None:
        raise HTTPException(
            400, detail="At least one of content or priority must be provided"
        )

    try:
        result = await _memory_client.update_existing(
            f"{req.domain}://{req.path}",
            content=req.content,
            priority=req.priority,
        )
        return {"ok": True, "path": req.path, "domain": req.domain, "result": result}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("Memory update error")
        raise HTTPException(500, detail=str(exc))


# -- DELETE /memory/delete ---------------------------------------------------


@app.delete("/memory/delete")
async def memory_delete(
    path: str = Query(..., description="Memory path to delete"),
    domain: str = Query("core", description="Memory domain"),
):
    """Delete (forget) a memory by its path."""
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        uri = f"{domain}://{path}"
        result = await _memory_client.forget(uri)
        return {"ok": True, "uri": uri, "result": result}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("Memory delete error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/children ---------------------------------------------------


@app.get("/memory/children")
async def memory_children(
    node_uuid: str = Query("", description="Parent node UUID (empty = root)"),
    domain: str = Query("core", description="Context domain"),
    path: str = Query("", description="Context path"),
):
    """List direct children of a memory node."""
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        from omicsclaw.memory.models import ROOT_NODE_UUID

        # Normalise FastAPI ``Query(...)`` defaults so the endpoint
        # behaves sanely when called directly (tests, in-process
        # tooling) rather than only over HTTP. Without this, ``path``
        # is a ``Query`` instance that is truthy and would corrupt the
        # URI build below.
        node_uuid_str = node_uuid if isinstance(node_uuid, str) else ""
        domain_str = domain if isinstance(domain, str) else "core"
        path_str = path if isinstance(path, str) else ""

        parent_uuid = node_uuid_str if node_uuid_str else ROOT_NODE_UUID
        # URI-based lookup: ``domain://`` resolves to ROOT, ``domain://path``
        # to the path's child node. Front-end sends consistent
        # ``(node_uuid, domain, path)`` triples returned from a previous
        # list_children_rich call so the URI lookup hits the same parent.
        parent_uri = f"{domain_str}://{path_str}" if path_str else f"{domain_str}://"
        children = await _memory_client.list_children_rich(
            parent_uri,
            context_domain=domain_str,
            context_path=path_str or None,
            include_shared=True,
        )
        await _decorate_analysis_titles(children)
        return {"node_uuid": parent_uuid, "domain": domain_str, "children": children}
    except Exception as exc:
        logger.exception("Memory children error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/domains ----------------------------------------------------


@app.get("/memory/domains")
async def memory_domains(
    namespace: Optional[str] = Query(
        None,
        description=(
            "Optional override. Omit for the desktop user's own scope "
            "(its namespace + ``__shared__``). Pass a concrete namespace "
            "(``app/foo``) to inspect that partition + shared. Pass an "
            "empty string for the admin view — every partition aggregated."
        ),
    ),
):
    """List memory domains with node counts.

    Default scope mirrors what the desktop chat user can address. Pass
    ``namespace=`` (empty) for an admin/debug view that surfaces data
    written under any partition, including stale launch-id namespaces
    or other multi-user slots.
    """
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        from omicsclaw.memory import get_memory_engine

        ns_value = namespace if isinstance(namespace, str) else None
        if ns_value is None:
            # Default: desktop user's reachable paths (own ns + shared).
            all_paths = await _memory_client.list_paths(
                domain=None,
                include_shared=True,
            )
        elif ns_value == "":
            # Admin view: aggregate every partition.
            all_paths = await get_memory_engine().list_paths(
                namespace=None,
                domain=None,
            )
        else:
            # Operator inspecting a specific partition.
            all_paths = await get_memory_engine().list_paths(
                namespace=ns_value,
                domain=None,
                include_shared=True,
            )

        # Group by domain and count
        domain_counts: dict[str, int] = {}
        for p in all_paths:
            d = p.get("domain", "core")
            domain_counts[d] = domain_counts.get(d, 0) + 1

        domains = [
            {"domain": d, "node_count": c} for d, c in sorted(domain_counts.items())
        ]
        return {"domains": domains, "total_nodes": len(all_paths)}
    except Exception as exc:
        logger.exception("Memory domains error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/namespaces --------------------------------------------------


@app.get("/memory/namespaces")
async def memory_namespaces():
    """List every namespace currently holding memory, plus the desktop's
    own namespace.

    Powers the admin/debug dropdown that lets an operator inspect a
    specific partition via ``/memory/domains?namespace=<value>``. The
    ``current`` field is the namespace the running ``_memory_client``
    is bound to — so the UI can preselect it.
    """
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        from omicsclaw.memory import desktop_namespace, get_memory_engine

        names = await get_memory_engine().list_namespaces()
        return {
            "namespaces": names,
            "current": desktop_namespace(),
        }
    except Exception as exc:
        logger.exception("Memory namespaces error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/recent ------------------------------------------------------


@app.get("/memory/recent")
async def memory_recent(
    limit: int = Query(10, ge=1, le=100, description="Max results"),
):
    """Get recently updated memories."""
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        # Desktop UI mode: include_shared=True surfaces user-customised
        # core://agent/* alongside per-namespace rows. Multi-tenant bot
        # surfaces use the default (strict) so one user's shared writes
        # don't bleed into another's view.
        results = await _memory_client.get_recent(limit=limit, include_shared=True)
        return {"results": results, "count": len(results)}
    except Exception as exc:
        logger.exception("Memory recent error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/review/changes ----------------------------------------------


@app.get("/memory/review/changes")
async def memory_review_changes():
    """Get pending AI-made changes awaiting review."""
    try:
        store = _get_changeset_store()
        changed_rows = store.get_changed_rows()
        return {
            "count": len(changed_rows),
            "changes": changed_rows,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Memory review changes error")
        raise HTTPException(500, detail=str(exc))


# -- POST /memory/review/approve ---------------------------------------------


@app.post("/memory/review/approve")
async def memory_review_approve():
    """Approve and clear all pending changes (integrate into memory)."""
    try:
        store = _get_changeset_store()
        count = store.clear_all()
        return {"ok": True, "cleared": count}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Memory review approve error")
        raise HTTPException(500, detail=str(exc))


# -- POST /memory/review/rollback --------------------------------------------


@app.post("/memory/review/rollback")
async def memory_review_rollback(req: MemoryRollbackRequest):
    """Rollback a specific memory to a previous version.

    PR #5: routed through ReviewLog instead of GraphService. Namespace
    is the desktop's launch-derived namespace so the rollback is
    confined to whatever partition the desktop user is editing.
    """
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        from omicsclaw.memory import desktop_namespace

        review = _get_review_log()
        result = await review.rollback_to(
            req.target_memory_id, namespace=desktop_namespace()
        )
        return {
            "ok": True,
            "result": {
                "restored_memory_id": result.restored_memory_id,
                "node_uuid": result.node_uuid,
                "was_already_active": result.was_already_active,
            },
        }
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Memory review rollback error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/review/orphans ----------------------------------------------


@app.get("/memory/review/orphans")
async def memory_review_orphans(
    namespace: Optional[str] = Query(
        None,
        description=(
            "Restrict to one namespace. Omit for the desktop's own "
            "namespace; pass an empty string for the global view."
        ),
    ),
):
    """List deprecated memories whose successor was deleted (orphan chains).

    Default namespace is the desktop's own; an explicit empty string
    means "across all partitions" (admin view).
    """
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        from dataclasses import asdict

        from omicsclaw.memory import desktop_namespace

        review = _get_review_log()

        if namespace is None:
            ns_filter: Optional[str] = desktop_namespace()
        elif namespace == "":
            ns_filter = None
        else:
            ns_filter = namespace

        orphans = await review.list_orphans(namespace=ns_filter)
        return {
            "namespace": ns_filter,
            "count": len(orphans),
            "orphans": [asdict(o) for o in orphans],
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Memory review orphans error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/review/version-chain ----------------------------------------


@app.get("/memory/review/version-chain")
async def memory_review_version_chain(
    uri: str = Query(..., description="Memory URI (e.g. 'core://agent')"),
    namespace: Optional[str] = Query(
        None,
        description="Override the desktop default namespace.",
    ),
):
    """List the version chain for a versioned URI in age order.

    Returns 400 if the URI is overwrite-only (``dataset://``,
    ``analysis://``) — those structurally cannot have a chain.
    """
    if _memory_client is None:
        raise HTTPException(503, detail="Memory system not available")

    try:
        from dataclasses import asdict

        from omicsclaw.memory import desktop_namespace
        from omicsclaw.memory.review_log import NoVersionHistoryError

        review = _get_review_log()
        ns = namespace or desktop_namespace()
        chain = await review.list_version_chain(uri, namespace=ns)
        return {
            "uri": uri,
            "namespace": ns,
            "count": len(chain),
            "entries": [asdict(e) for e in chain],
        }
    except NoVersionHistoryError as exc:
        raise HTTPException(400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Memory review version-chain error")
        raise HTTPException(500, detail=str(exc))


# -- POST /memory/review/clear -----------------------------------------------


@app.post("/memory/review/clear")
async def memory_review_clear():
    """Discard all pending changes by rolling memory state back to snapshots."""
    try:
        result = await _discard_pending_memory_review_changes()
        return {"ok": True, "cleared": result["discarded"], **result}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("Memory review clear error")
        raise HTTPException(500, detail=str(exc))


# -- POST /memory/glossary/add -----------------------------------------------


@app.post("/memory/glossary/add")
async def memory_glossary_add(req: GlossaryRequest):
    """Bind a glossary keyword to a memory node."""
    try:
        glossary = _get_glossary_service()
        result = await glossary.add_glossary_keyword(
            keyword=req.keyword,
            node_uuid=req.node_uuid,
        )
        return {"ok": True, "result": result}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Glossary add error")
        raise HTTPException(500, detail=str(exc))


# -- DELETE /memory/glossary/remove -------------------------------------------


@app.delete("/memory/glossary/remove")
async def memory_glossary_remove(req: GlossaryRequest):
    """Remove a glossary keyword binding from a memory node."""
    try:
        glossary = _get_glossary_service()
        result = await glossary.remove_glossary_keyword(
            keyword=req.keyword,
            node_uuid=req.node_uuid,
        )
        return {"ok": True, "result": result}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Glossary remove error")
        raise HTTPException(500, detail=str(exc))


# -- GET /memory/scoped -------------------------------------------------------


@app.get("/memory/scoped")
async def memory_scoped_list(
    workspace: str = Query("", description="Workspace directory to scan"),
    scope: str = Query("", description="Filter by scope (user, project, etc.)"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
):
    """List scoped memory files from the workspace's .omicsclaw/scoped_memory/ directory."""
    try:
        from omicsclaw.memory.scoped_memory import resolve_scoped_memory_root
        from omicsclaw.memory.scoped_memory_index import scan_scoped_memory_headers
    except ImportError:
        raise HTTPException(503, detail="Scoped memory module not available")

    ws = _resolve_scoped_memory_workspace(workspace)

    if not ws:
        return {"records": [], "count": 0, "error": "No workspace configured"}

    root = resolve_scoped_memory_root(workspace_dir=ws)
    if root is None or not root.exists():
        return {"records": [], "count": 0, "root": str(root) if root else None}

    headers = scan_scoped_memory_headers(root, scope=scope, limit=limit)

    records = []
    for h in headers:
        records.append(
            {
                "memory_id": h.memory_id,
                "scope": h.scope,
                "title": h.title,
                "description": h.description,
                "owner": h.owner,
                "freshness": h.freshness,
                "updated_at": h.updated_at,
                "created_at": h.created_at,
                "domain": h.domain,
                "keywords": list(h.keywords),
                "dataset_refs": list(h.dataset_refs),
                "path": str(h.path),
                "relative_path": h.relative_path,
            }
        )

    return {"records": records, "count": len(records), "root": str(root)}


# -- POST /memory/scoped/prune -----------------------------------------------


@app.post("/memory/scoped/prune")
async def memory_scoped_prune(req: ScopedPruneRequest):
    """Prune stale or duplicate scoped memories."""
    try:
        from omicsclaw.memory.scoped_memory import prune_scoped_memories
    except ImportError:
        raise HTTPException(503, detail="Scoped memory module not available")

    ws = _resolve_scoped_memory_workspace(req.workspace)

    if not ws:
        raise HTTPException(
            400, detail="No workspace configured; provide 'workspace' in request body"
        )

    try:
        result = prune_scoped_memories(
            workspace_dir=ws,
            scope=req.scope,
            apply_changes=req.apply,
        )
        candidates = []
        for c in result.candidates:
            candidates.append(
                {
                    "memory_id": c.record.memory_id,
                    "title": c.record.title,
                    "scope": c.record.scope,
                    "reason": c.reason,
                    "path": str(c.record.path),
                }
            )
        return {
            "ok": True,
            "applied": req.apply,
            "scope": result.scope,
            "candidates": candidates,
            "deleted_count": result.deleted_count,
            "root": str(result.root),
        }
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("Scoped prune error")
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /settings — current OmicsClaw configuration
# ---------------------------------------------------------------------------


@app.get("/settings")
async def get_settings():
    core = _get_core()
    usage = core.get_usage_snapshot()
    uptime = int(time.time() - core.BOT_START_TIME)

    return {
        "provider": core.LLM_PROVIDER_NAME,
        "model": core.OMICSCLAW_MODEL,
        "max_history": core.MAX_HISTORY,
        "max_history_chars": core.MAX_HISTORY_CHARS,
        "max_tool_iterations": core.MAX_TOOL_ITERATIONS,
        **_skills_count_payload(core),
        "tools_count": len(core.get_tool_executors()),
        "data_dir": str(core.DATA_DIR),
        "output_dir": str(core.OUTPUT_DIR),
        "uptime_seconds": uptime,
        "usage": usage,
        "memory_enabled": _memory_client is not None,
    }


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _read_claude_settings() -> dict[str, Any]:
    settings_path = _claude_settings_path()
    try:
        if not settings_path.exists():
            return {}
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_claude_settings(settings: dict[str, Any]) -> None:
    settings_path = _claude_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


class ClaudeSettingsRequest(BaseModel):
    settings: dict[str, Any]


@app.get("/claude/settings")
async def get_claude_settings():
    return {"settings": _read_claude_settings()}


@app.put("/claude/settings")
async def put_claude_settings(req: ClaudeSettingsRequest):
    _write_claude_settings(req.settings)
    return {"success": True}


# ---------------------------------------------------------------------------
# GET /providers — list all supported LLM providers
# ---------------------------------------------------------------------------


def _read_first_env(*keys: str) -> str:
    for key in keys:
        value = str(os.environ.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _configured_provider_name() -> str:
    return _read_first_env("LLM_PROVIDER", "OMICSCLAW_PROVIDER").lower()


def _provider_base_url_source(provider_name: str, active_provider: str) -> str:
    explicit_provider = _configured_provider_name()
    provider_base_url = _read_first_env(f"{provider_name.upper()}_BASE_URL")
    if provider_base_url and explicit_provider == provider_name:
        return "provider-base-url"

    generic_base_url = _read_first_env("LLM_BASE_URL", "OMICSCLAW_BASE_URL")
    if generic_base_url and (
        explicit_provider == provider_name or active_provider == provider_name
    ):
        return "generic-base-url"

    return ""


def _provider_configuration_source(
    provider_name: str, env_key: str, active_provider: str
) -> str:
    explicit_provider = _configured_provider_name()

    if env_key and _read_first_env(env_key):
        return "provider-env"

    generic_key = _read_first_env("LLM_API_KEY", "OMICSCLAW_API_KEY")
    if explicit_provider == provider_name or active_provider == provider_name:
        if generic_key:
            return "generic-env"

        base_url_source = _provider_base_url_source(provider_name, active_provider)
        if provider_name == "custom" and base_url_source:
            return base_url_source
        if provider_name == "ollama":
            if explicit_provider == provider_name:
                return "explicit-provider"
            if base_url_source:
                return base_url_source

    return ""


@app.get("/providers")
async def list_providers():
    """List all supported LLM providers with current selection."""
    core = _get_core()

    try:
        from omicsclaw.providers.registry import (
            PROVIDER_PRESETS,
            build_provider_registry_entries,
        )
    except ImportError:
        return {
            "providers": [],
            "current": core.LLM_PROVIDER_NAME,
            "current_model": core.OMICSCLAW_MODEL,
        }

    discovered_models = await _cached_ollama_models(PROVIDER_PRESETS)

    providers = []
    try:
        provider_entries = build_provider_registry_entries(
            PROVIDER_PRESETS, discovered_models=discovered_models
        )
    except Exception:
        provider_entries = [
            {
                "name": name,
                "base_url": base_url,
                "default_model": default_model,
                "env_key": env_key,
                "display_name": name,
                "description": "",
                "description_zh": "",
                "tier": "local",
                "models": [default_model] if default_model else [],
                "model_metadata": (
                    [{"id": default_model, "context_window": None}]
                    if default_model
                    else []
                ),
            }
            for name, (base_url, default_model, env_key) in PROVIDER_PRESETS.items()
        ]

    try:
        from omicsclaw.providers.ccproxy import provider_supports_oauth
    except ImportError:

        def provider_supports_oauth(_name: str) -> bool:  # type: ignore[no-redef]
            return False

    oauth_statuses = _cached_oauth_statuses()

    for entry in provider_entries:
        name = str(entry.get("name", "") or "")
        env_key = str(entry.get("env_key", "") or "")
        active = name == core.LLM_PROVIDER_NAME
        entry_payload = dict(entry)
        base_url_source = _provider_base_url_source(name, core.LLM_PROVIDER_NAME)
        if base_url_source:
            entry_payload["base_url"] = (
                _read_first_env(f"{name.upper()}_BASE_URL")
                or _read_first_env("LLM_BASE_URL", "OMICSCLAW_BASE_URL")
                or str(entry_payload.get("base_url", "") or "")
            )
        if (
            active
            and core.OMICSCLAW_MODEL
            and not str(entry_payload.get("default_model", "") or "").strip()
        ):
            entry_payload["default_model"] = core.OMICSCLAW_MODEL
        credential_source = _provider_configuration_source(
            name, env_key, core.LLM_PROVIDER_NAME
        )
        oauth_supported = provider_supports_oauth(name)
        providers.append(
            {
                **entry_payload,
                "configured": bool(credential_source),
                "configured_via": credential_source or None,
                "active": active,
                "oauth_supported": oauth_supported,
                "oauth_authenticated": (
                    oauth_statuses.get(name, {}).get("authenticated", False)
                    if oauth_supported
                    else False
                ),
            }
        )

    return {
        "providers": providers,
        "current": core.LLM_PROVIDER_NAME,
        "current_model": core.OMICSCLAW_MODEL,
    }


# ---------------------------------------------------------------------------
# Ollama installed-model discovery cache
# ---------------------------------------------------------------------------
#
# Probes ``GET {ollama_base_url}/api/tags`` to surface the user's actually
# installed Ollama models in the UI rather than only the curated catalog.
# Resolves the gap behind https://github.com/TianGzlab/OmicsClaw/issues/208
# where freshly pulled tags (e.g. ``gemma3:4b``) never appeared in the
# dropdown. Falls back silently to the curated list if the daemon isn't
# running. Short TTL — installed models change rarely but settings pages
# may poll ``/providers`` frequently.

_OLLAMA_MODELS_CACHE: dict[str, object] = {"ts": 0.0, "data": {}}
_OLLAMA_MODELS_TTL_SECONDS: float = 30.0


def _normalize_ollama_discovery_base_url(raw: str) -> str:
    """Strip the OpenAI-compat ``/v1`` suffix from an Ollama base URL.

    The OmicsClaw preset advertises ``http://localhost:11434/v1`` because
    chat/completions traffic goes through Ollama's OpenAI-compatible layer.
    Native endpoints — including ``/api/tags`` used for installed-model
    discovery — live at the *root*, so passing the ``/v1``-suffixed value
    straight to :func:`discover_ollama_models_async` would request the
    nonexistent ``/v1/api/tags`` and silently 404. We only strip a trailing
    ``/v1`` segment; nothing else about the URL is rewritten.
    """
    value = str(raw or "").strip().rstrip("/")
    if value.endswith("/v1"):
        return value[:-3]
    return value


async def _cached_ollama_models(
    provider_presets: dict[str, tuple[str, str, str]],
) -> dict[str, list[str]]:
    """Return ``{provider_name: [model, ...]}`` from live Ollama discovery.

    Cached for ``_OLLAMA_MODELS_TTL_SECONDS``. Returns an empty mapping on
    any failure so the curated fallback list still drives the UI.
    """
    import time as _time

    now = _time.monotonic()
    if (now - float(_OLLAMA_MODELS_CACHE["ts"])) < _OLLAMA_MODELS_TTL_SECONDS:
        return _OLLAMA_MODELS_CACHE["data"]  # type: ignore[return-value]

    result: dict[str, list[str]] = {}
    try:
        from omicsclaw.providers.patches import discover_ollama_models_async

        ollama_preset = provider_presets.get("ollama")
        if ollama_preset:
            base_url = _normalize_ollama_discovery_base_url(
                _read_first_env("OLLAMA_BASE_URL")
                or (
                    _read_first_env("LLM_BASE_URL", "OMICSCLAW_BASE_URL")
                    if _configured_provider_name() == "ollama"
                    else ""
                )
                or str(ollama_preset[0] or "")
            )
            if base_url:
                models = await discover_ollama_models_async(base_url)
                if models:
                    result["ollama"] = models
    except Exception:
        result = {}

    _OLLAMA_MODELS_CACHE["ts"] = now
    _OLLAMA_MODELS_CACHE["data"] = result
    return result


# ---------------------------------------------------------------------------
# OAuth status cache — avoids subprocess-spawning on every /providers poll
# ---------------------------------------------------------------------------

_OAUTH_STATUS_CACHE: dict[str, object] = {"ts": 0.0, "data": {}}
_OAUTH_STATUS_TTL_SECONDS: float = 30.0


def _cached_oauth_statuses() -> dict[str, dict[str, object]]:
    """Return per-provider OAuth status, refreshed at most every 30s.

    Returns ``{}`` if ccproxy is not installed — the ``/providers`` endpoint
    then falls back to ``oauth_authenticated: False`` uniformly.
    """
    import time as _time

    now = _time.monotonic()
    if (now - float(_OAUTH_STATUS_CACHE["ts"])) < _OAUTH_STATUS_TTL_SECONDS:
        return _OAUTH_STATUS_CACHE["data"]  # type: ignore[return-value]

    result: dict[str, dict[str, object]] = {}
    try:
        from omicsclaw.providers.ccproxy import (
            OAUTH_PROVIDERS,
            check_ccproxy_auth,
            is_ccproxy_available,
        )

        if is_ccproxy_available():
            for p in OAUTH_PROVIDERS.values():
                ok, msg = check_ccproxy_auth(p.ccproxy_target)
                result[p.omics_name] = {"authenticated": ok, "message": msg}
    except Exception:
        result = {}

    _OAUTH_STATUS_CACHE["ts"] = now
    _OAUTH_STATUS_CACHE["data"] = result
    return result


# ---------------------------------------------------------------------------
# PUT /providers — switch LLM provider
# ---------------------------------------------------------------------------


class ProviderSwitchRequest(BaseModel):
    provider: str
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    auth_mode: str = "api_key"  # "api_key" | "oauth"
    ccproxy_port: int = 11435


class ProviderTestRequest(BaseModel):
    provider: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""


def _validate_custom_provider_input(
    *,
    provider: str,
    model: str = "",
    base_url: str = "",
    allow_env_base_url: bool = False,
) -> None:
    if str(provider or "").strip().lower() != "custom":
        return
    if not str(model or "").strip():
        raise HTTPException(
            400,
            detail="Custom Endpoint requires an explicit model name.",
        )
    if str(base_url or "").strip():
        return
    if allow_env_base_url and _read_first_env("LLM_BASE_URL", "OMICSCLAW_BASE_URL"):
        return
    raise HTTPException(
        400,
        detail="Custom Endpoint requires an explicit Base URL.",
    )


@app.put("/providers")
async def switch_provider(req: ProviderSwitchRequest):
    """Switch the active LLM provider. Re-initializes the core LLM client.

    When ``auth_mode="oauth"`` is requested (valid only for ``anthropic``
    / ``openai``), the backend spawns/reuses a local ccproxy server and
    routes requests through it — no API key is required.
    """
    core = _get_core()

    # Validate provider name
    try:
        from omicsclaw.providers.registry import PROVIDER_PRESETS
        from omicsclaw.providers.ccproxy import provider_supports_oauth

        if req.provider not in PROVIDER_PRESETS and req.provider != "custom":
            raise HTTPException(400, detail=f"Unknown provider: {req.provider}")
    except ImportError:

        def provider_supports_oauth(_name: str) -> bool:  # type: ignore[no-redef]
            return False

    # Validate auth mode
    auth_mode = str(req.auth_mode or "api_key").strip().lower()
    if auth_mode not in ("api_key", "oauth"):
        raise HTTPException(
            400, detail=f"Invalid auth_mode: {req.auth_mode} (expected api_key|oauth)"
        )
    if auth_mode == "oauth" and not provider_supports_oauth(req.provider):
        raise HTTPException(
            400,
            detail=(
                f"auth_mode=oauth is not supported for provider '{req.provider}'. "
                "Supported: anthropic, openai"
            ),
        )
    _validate_custom_provider_input(
        provider=req.provider,
        model=req.model,
        base_url=req.base_url,
    )

    # Reject ccproxy_port == desktop-server's own port: ccproxy serve would
    # fail to bind (the desktop-server already owns the port), leaving the
    # switch attempt in a broken half-state.
    requested_port = int(req.ccproxy_port or 11435)
    if auth_mode == "oauth":
        app_port = _current_app_server_port()
        if requested_port == app_port:
            raise HTTPException(
                400,
                detail=_oauth_port_conflict_message(requested_port, app_port),
            )

    # Re-init core with new provider. When auth_mode=oauth, api_key can be
    # empty — ccproxy supplies the OAuth token. core.init() will raise if
    # ccproxy is missing or unauthenticated.
    api_key = req.api_key if req.api_key else os.environ.get("LLM_API_KEY", "")
    if auth_mode == "oauth":
        api_key = ""  # force ccproxy sentinel path inside core.init
    try:
        core.init(
            api_key=api_key,
            base_url=req.base_url or None,
            model=req.model,
            provider=req.provider,
            auth_mode=auth_mode,
            ccproxy_port=requested_port,
            # Explicit user action via PUT /providers: fail loudly so the
            # frontend can surface a precise error message.
            strict_oauth=True,
        )
    except Exception as exc:
        raise HTTPException(500, detail=f"Failed to switch provider: {exc}")

    # Persist to .env
    env_path = _get_omicsclaw_env_path()
    if env_path:
        updates: dict[str, str] = {
            "LLM_PROVIDER": core.LLM_PROVIDER_NAME or req.provider,
            "LLM_AUTH_MODE": auth_mode,
        }
        if core.OMICSCLAW_MODEL:
            updates["OMICSCLAW_MODEL"] = core.OMICSCLAW_MODEL
        if req.api_key:
            updates["LLM_API_KEY"] = req.api_key
        if req.base_url and auth_mode != "oauth":
            updates["LLM_BASE_URL"] = req.base_url
        remove_keys: set[str] = set()
        if auth_mode == "oauth":
            updates["CCPROXY_PORT"] = str(requested_port)
            remove_keys.update({"LLM_BASE_URL", "OMICSCLAW_BASE_URL"})
        else:
            remove_keys.add("CCPROXY_PORT")
            if req.provider != "custom" and not req.base_url:
                remove_keys.update({"LLM_BASE_URL", "OMICSCLAW_BASE_URL"})
        _update_env_file(env_path, updates, remove_keys=remove_keys)

    logger.info(
        "Switched to provider=%s model=%s auth_mode=%s",
        core.LLM_PROVIDER_NAME,
        core.OMICSCLAW_MODEL,
        auth_mode,
    )

    return {
        "ok": True,
        "provider": core.LLM_PROVIDER_NAME,
        "model": core.OMICSCLAW_MODEL,
        "auth_mode": auth_mode,
    }


@app.post("/providers/test")
async def test_provider(req: ProviderTestRequest):
    """Run a minimal live test against an OpenAI-compatible provider config."""
    core = _get_core()
    provider = str(req.provider or getattr(core, "LLM_PROVIDER_NAME", "") or "").strip()
    model = str(req.model or getattr(core, "OMICSCLAW_MODEL", "") or "").strip()
    base_url = str(req.base_url or "").strip()
    api_key = str(req.api_key or "").strip()

    _validate_custom_provider_input(
        provider=provider,
        model=model,
        base_url=base_url,
        allow_env_base_url=True,
    )

    try:
        from omicsclaw.providers.runtime import (
            provider_requires_api_key,
            resolve_provider_runtime,
        )
    except Exception as exc:
        raise HTTPException(500, detail=f"Provider runtime unavailable: {exc}") from exc

    runtime = resolve_provider_runtime(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
    )
    if not runtime.model:
        raise HTTPException(400, detail="No model resolved for provider test.")
    if provider_requires_api_key(runtime.provider) and not runtime.api_key:
        raise HTTPException(400, detail="No API key resolved for provider test.")

    start = time.monotonic()
    client = AsyncOpenAI(
        api_key=runtime.api_key,
        base_url=runtime.base_url or None,
        timeout=15.0,
    )
    try:
        response = await client.chat.completions.create(
            model=runtime.model,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=64,
        )
        choice = response.choices[0]
        message = choice.message
        content = str(getattr(message, "content", "") or "").strip()
        # Reasoning models (DeepSeek-R1, etc.) may emit final text in a
        # non-standard `reasoning_content` field while leaving `content`
        # empty when the token budget is consumed by chain-of-thought.
        reasoning = str(getattr(message, "reasoning_content", "") or "").strip()
        tool_calls = getattr(message, "tool_calls", None) or []
        finish_reason = str(getattr(choice, "finish_reason", "") or "").strip()
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "status": "failed",
            "provider": runtime.provider,
            "model": runtime.model,
            "base_url": runtime.base_url,
            "message": f"Live provider test failed: {exc}",
            "duration_ms": duration_ms,
        }
    finally:
        with suppress(Exception):
            await client.close()

    duration_ms = int((time.monotonic() - start) * 1000)
    has_signal = bool(content) or bool(reasoning) or bool(tool_calls)
    if not has_signal:
        hint = f" (finish_reason={finish_reason})" if finish_reason else ""
        return {
            "ok": False,
            "status": "failed",
            "provider": runtime.provider,
            "model": runtime.model,
            "base_url": runtime.base_url,
            "message": f"Live provider test returned no content{hint}.",
            "duration_ms": duration_ms,
        }

    detail_bits: list[str] = []
    if content:
        detail_bits.append("content")
    if reasoning:
        detail_bits.append("reasoning_content")
    if tool_calls:
        detail_bits.append("tool_calls")
    if finish_reason:
        detail_bits.append(f"finish_reason={finish_reason}")
    detail = ", ".join(detail_bits)

    return {
        "ok": True,
        "status": "passed",
        "provider": runtime.provider,
        "model": runtime.model,
        "base_url": runtime.base_url,
        "message": "Live provider test passed.",
        "detail": detail,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# Session titling — summarise a conversation into a short sidebar title
# ---------------------------------------------------------------------------


class ChatTitleMessage(BaseModel):
    role: str = ""
    content: str = ""


class ChatTitleRequest(BaseModel):
    messages: list[ChatTitleMessage] = Field(default_factory=list)


_TITLE_SYSTEM_PROMPT = (
    "You name a bioinformatics analysis conversation for a sidebar. "
    "Read the conversation and reply with ONE short title that captures the "
    "main task, and the method and dataset when they are clear. "
    "Keep it under 6 words or 20 Chinese characters. "
    "Reply in the language the user writes in. "
    "Reply with ONLY the title — no quotes, no trailing punctuation, no prefix "
    "like 'Title:'."
)

_TITLE_MESSAGE_CHAR_LIMIT = 600
_TITLE_MAX_TURNS = 8


def _build_title_transcript(messages: list[ChatTitleMessage]) -> str:
    """Render cleaned turns into a compact transcript for the titler. The
    frontend already flattens structured content to plain text, so here we
    only label roles, trim very long turns, and cap the number of turns."""
    lines: list[str] = []
    for message in messages[:_TITLE_MAX_TURNS]:
        text = str(message.content or "").strip()
        if not text:
            continue
        if len(text) > _TITLE_MESSAGE_CHAR_LIMIT:
            text = text[:_TITLE_MESSAGE_CHAR_LIMIT].rstrip() + "…"
        speaker = "User" if message.role == "user" else "Assistant"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _sanitize_generated_title(raw: str) -> str:
    """Strip the quoting / prefixes models sometimes wrap a title in."""
    title = str(raw or "").strip()
    if not title:
        return ""
    # Single-line only; models occasionally add reasoning on later lines.
    title = title.splitlines()[0].strip()
    # Drop a leading "Title:" / "标题：" style prefix.
    for prefix in ("title:", "标题:", "标题："):
        if title.lower().startswith(prefix):
            title = title[len(prefix) :].strip()
            break
    # Unwrap surrounding quotes / brackets.
    title = title.strip("\"'“”‘’《》「」 ").strip()
    return title


@app.post("/chat/title", dependencies=[Depends(require_bearer_token)])
async def chat_title(req: ChatTitleRequest):
    """Generate a concise conversation title via a single cheap completion.

    Reuses the core's already-authenticated LLM client (the same provider /
    credentials the chat turn uses), so titling works without re-resolving
    credentials from the environment. Best-effort by contract: the desktop app
    falls back to its local heuristic when this returns an error, so failures
    here are surfaced as HTTP errors rather than silently returning a bad title.
    """
    core = _get_core()
    transcript = _build_title_transcript(req.messages)
    if not transcript.strip():
        raise HTTPException(400, detail="No conversation content to title.")

    client = getattr(core, "llm", None)
    model = str(getattr(core, "OMICSCLAW_MODEL", "") or "").strip()
    if client is None or not model:
        raise HTTPException(
            400, detail="LLM client is not configured for title generation."
        )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_tokens=64,
        )
        raw = str(getattr(response.choices[0].message, "content", "") or "")
    except Exception as exc:
        raise HTTPException(502, detail=f"Title generation failed: {exc}") from exc

    title = _sanitize_generated_title(raw)
    if not title:
        raise HTTPException(502, detail="Title generation returned no content.")
    return {"title": title}


# ---------------------------------------------------------------------------
# OAuth endpoints — Claude Pro/Max + OpenAI Codex login via ccproxy
# ---------------------------------------------------------------------------


def _resolve_oauth_provider_alias(name: str) -> str:
    """Resolve any CLI/omics/ccproxy alias → OmicsClaw canonical name.

    Delegates to ``ccproxy_manager.normalize_oauth_provider`` (the single
    source of truth) and re-raises as HTTP 400 for FastAPI callers.
    """
    from omicsclaw.providers.ccproxy import normalize_oauth_provider

    try:
        return normalize_oauth_provider(name)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))


# Proxy env vars httpx / requests / curl honor. Listed in both the lower-
# and upper-case forms the stdlib / httpx actually consult.
_PROXY_ENV_VAR_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def _empty_proxy_env_vars() -> list[str]:
    """Return proxy env var names currently set to the empty string.

    Empty-string values (``HTTPS_PROXY=""``) are httpx's #1 footgun: it
    treats them as "proxy configured with empty URL" and raises
    ``ValueError: Unknown scheme for proxy URL URL('')`` the moment any
    httpx client is constructed. This is exactly what happens when users
    launch the server with ``NO_PROXY=* HTTPS_PROXY= HTTP_PROXY= ...`` to
    bypass a system proxy — the emptiness propagates into every subprocess
    we spawn, including ``ccproxy auth login``.
    """
    return [v for v in _PROXY_ENV_VAR_NAMES if os.environ.get(v, None) == ""]


def _wrap_with_env_unset(command: str, vars_to_unset: list[str]) -> str:
    """Prefix ``command`` with ``env -u VAR ...`` for each name in
    ``vars_to_unset``. Returns ``command`` unchanged if the list is empty.
    """
    if not vars_to_unset:
        return command
    unset_flags = " ".join(f"-u {v}" for v in vars_to_unset)
    return f"env {unset_flags} {command}"


def _require_ccproxy_available() -> None:
    try:
        from omicsclaw.providers.ccproxy import (
            ccproxy_diagnostic_hint,
            is_ccproxy_available,
            oauth_install_hint,
        )
    except Exception as exc:
        raise HTTPException(500, detail=f"ccproxy_manager import failed: {exc}")
    if not is_ccproxy_available():
        raise HTTPException(
            400,
            detail=(
                "ccproxy is not installed (from the server process's "
                "perspective).\n\n"
                f"{ccproxy_diagnostic_hint()}\n\n"
                f"Install: {oauth_install_hint()}"
            ),
        )


@app.get("/auth/{provider}/status")
async def oauth_status(provider: str):
    """Return the current OAuth credential status for ``provider``.

    Cached up to 30s. Does not start ccproxy serve mode.
    """
    omics_name = _resolve_oauth_provider_alias(provider)
    _require_ccproxy_available()
    from omicsclaw.providers.ccproxy import check_ccproxy_auth

    # check_ccproxy_auth accepts any alias; pass canonical for clarity.
    ok, msg = check_ccproxy_auth(omics_name)
    # invalidate cache so the next /providers poll reflects this probe
    _OAUTH_STATUS_CACHE["ts"] = 0.0
    return {"provider": omics_name, "authenticated": ok, "message": msg}


@app.post("/auth/{provider}/login")
async def oauth_login(provider: str):
    """Return the shell command the user should run to complete OAuth.

    We intentionally do NOT spawn ``ccproxy auth login`` on the server
    process's behalf — it triggers an interactive browser flow, and the
    only machine whose filesystem needs the resulting credentials is the
    one where the backend's ``ccproxy serve`` will read them from (its own
    host). For Docker / remote deployments, the user must SSH or
    ``docker exec`` into the backend host before running this command;
    logging in on a different machine writes the credentials to the wrong
    filesystem and leaves the backend unauthenticated.
    """
    omics_name = _resolve_oauth_provider_alias(provider)
    _require_ccproxy_available()
    from omicsclaw.providers.ccproxy import get_oauth_provider

    target = get_oauth_provider(omics_name).ccproxy_target
    empty_proxies = _empty_proxy_env_vars()
    base_cmd = f"ccproxy auth login {target}"
    response: dict[str, object] = {
        "provider": omics_name,
        "command": _wrap_with_env_unset(base_cmd, empty_proxies),
        "hint": (
            "Run this command on the host where the OmicsClaw backend is "
            "running. For Docker or remote deployments, SSH or `docker "
            "exec` into that host first — ccproxy stores OAuth credentials "
            "on whatever machine executes the login, and the backend reads "
            "them from its own host."
        ),
    }
    if empty_proxies:
        response["warning"] = (
            f"Backend detected empty proxy env vars "
            f"({', '.join(empty_proxies)}) inherited by the server process. "
            "httpx inside ccproxy would reject these as invalid proxy URLs "
            "('Unknown scheme for proxy URL URL(\"\")'). The command above "
            "prepends `env -u` to neutralize them for the ccproxy subprocess. "
            "Long-term, unset them in your shell config so every terminal "
            "starts clean."
        )
    return response


@app.post("/auth/{provider}/logout")
async def oauth_logout(provider: str):
    """Invoke ``ccproxy auth logout`` for the given provider."""
    omics_name = _resolve_oauth_provider_alias(provider)
    _require_ccproxy_available()
    from omicsclaw.providers.ccproxy import (
        ccproxy_executable,
        clear_ccproxy_env,
        get_oauth_provider,
    )
    from omicsclaw.providers.runtime import (
        clear_active_provider_runtime,
        get_active_provider_runtime,
    )
    import subprocess as _sp

    target = get_oauth_provider(omics_name).ccproxy_target
    try:
        result = _sp.run(
            [ccproxy_executable(), "auth", "logout", target],
            capture_output=True,
            text=True,
            timeout=10,
            env=scrub_internal_control_credentials(os.environ),
        )
    except Exception as exc:
        raise HTTPException(500, detail=f"Failed to run ccproxy logout: {exc}")

    # Clear ccproxy env injection so subsequent API-key requests don't
    # keep routing through the (now logged-out) local proxy.
    clear_ccproxy_env(omics_name)
    runtime = get_active_provider_runtime()
    if (
        runtime is not None
        and runtime.auth_mode == "oauth"
        and runtime.provider == omics_name
    ):
        clear_active_provider_runtime()
        try:
            core = _get_core()
            core.llm = None
            # Drop the provider display state too. Otherwise the frontend
            # would keep showing this (now credential-less) OAuth provider
            # as active, and the next chat request would land on
            # `llm is None` without any hint that re-auth is required.
            core.LLM_PROVIDER_NAME = ""
            core.OMICSCLAW_MODEL = ""
        except Exception:
            pass

        # Persist the fallback so a restart doesn't re-enter oauth mode
        # with stale LLM_AUTH_MODE=oauth / CCPROXY_PORT in .env.
        env_path = _get_omicsclaw_env_path()
        if env_path:
            _update_env_file(
                env_path,
                {"LLM_AUTH_MODE": "api_key"},
                remove_keys={"CCPROXY_PORT"},
            )

    _OAUTH_STATUS_CACHE["ts"] = 0.0  # invalidate cache
    return {
        "provider": omics_name,
        "ok": result.returncode == 0,
        "message": (result.stdout + result.stderr).strip() or "logged out",
    }


# ---------------------------------------------------------------------------
# MCP Server Management — bridges frontend config with OmicsClaw runtime
# ---------------------------------------------------------------------------


@app.get("/mcp/servers")
async def mcp_list_servers():
    """List all configured MCP servers and their active/probe status."""
    try:
        from omicsclaw.surfaces.cli._mcp import list_mcp_servers

        servers = list_mcp_servers()
    except ImportError:
        return {"servers": [], "error": "MCP module not available"}
    except Exception as exc:
        logger.warning("mcp list error: %s", exc)
        return {"servers": [], "error": str(exc)}

    # Probe active status
    active_entries = ()
    if _mcp_load_fn is not None:
        try:
            active_entries = await _mcp_load_fn()
        except Exception:
            pass

    active_names = set()
    for entry in active_entries:
        if isinstance(entry, dict):
            active_names.add(entry.get("name", ""))
        elif isinstance(entry, str):
            active_names.add(entry)

    result = []
    for srv in servers:
        name = srv.get("name", "")
        enabled = bool(srv.get("enabled", True))
        result.append(
            {
                "name": name,
                "transport": srv.get("transport", "stdio"),
                "type": srv.get("transport", "stdio"),
                "target": srv.get("command") or srv.get("url", ""),
                "command": srv.get("command", ""),
                "url": srv.get("url", ""),
                "active": enabled and name in active_names,
                "enabled": enabled,
                "extra_args": srv.get("args", []),
                "args": srv.get("args", []),
                "env": srv.get("env", {}),
                "headers": srv.get("headers", {}),
                "header_keys": sorted((srv.get("headers") or {}).keys()),
                "tools": srv.get("tools"),
            }
        )

    return {"servers": result}


class McpAddRequest(BaseModel):
    name: str
    target: str
    transport: str = ""
    extra_args: list[str] = []
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    enabled: bool | None = None
    tools: list[str] = []


@app.post("/mcp/servers")
async def mcp_add_server(req: McpAddRequest):
    """Add or update an MCP server in OmicsClaw config."""
    try:
        from omicsclaw.surfaces.cli._mcp import add_mcp_server

        add_mcp_server(
            name=req.name,
            target=req.target,
            transport=req.transport or None,
            extra_args=req.extra_args or None,
            env=req.env or None,
            headers=req.headers or None,
            enabled=req.enabled,
            tools=req.tools or None,
        )
        return {"ok": True, "name": req.name}
    except ImportError:
        raise HTTPException(503, detail="MCP module not available")
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))


@app.delete("/mcp/servers/{name}")
async def mcp_remove_server(name: str):
    """Remove an MCP server from OmicsClaw config."""
    try:
        from omicsclaw.surfaces.cli._mcp import remove_mcp_server

        removed = remove_mcp_server(name)
        if not removed:
            raise HTTPException(404, detail=f"Server '{name}' not found")
        return {"ok": True, "name": name}
    except ImportError:
        raise HTTPException(503, detail="MCP module not available")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))


def _reconcile_mcp_servers(incoming: Any) -> dict[str, Any]:
    try:
        from omicsclaw.surfaces.cli._mcp import (
            add_mcp_server,
            list_mcp_servers,
            remove_mcp_server,
        )
    except ImportError:
        raise HTTPException(503, detail="MCP module not available")

    if incoming is None:
        raise HTTPException(400, detail="mcpServers is required")
    if not isinstance(incoming, dict):
        raise HTTPException(400, detail="mcpServers must be an object")

    desired_servers: list[dict[str, Any]] = []
    for raw_name, config in incoming.items():
        name = str(raw_name or "").strip()
        if not name:
            raise HTTPException(400, detail="MCP server names must not be empty")
        if not isinstance(config, dict):
            raise HTTPException(
                400, detail=f"Config for MCP server '{name}' must be an object"
            )

        cmd = config.get("command", "")
        url = config.get("url", "")
        target = str(cmd or url or "").strip()
        if not target:
            raise HTTPException(
                400,
                detail=f"MCP server '{name}' is missing both command and url",
            )

        desired_servers.append(
            {
                "name": name,
                "target": target,
                "transport": config.get("type") or config.get("transport", ""),
                "extra_args": config.get("args", []),
                "env": config.get("env", {}),
                "headers": config.get("headers", {}),
                "enabled": config.get("enabled") if "enabled" in config else None,
                "tools": config.get("tools", []),
            }
        )

    existing = {
        str(server.get("name", "")).strip(): server
        for server in list_mcp_servers()
        if str(server.get("name", "")).strip()
    }
    desired_names = {server["name"] for server in desired_servers}

    removed = 0
    for name in sorted(set(existing) - desired_names):
        if remove_mcp_server(name):
            removed += 1

    synced = 0
    for server in desired_servers:
        add_mcp_server(
            name=server["name"],
            target=server["target"],
            transport=server["transport"] or None,
            extra_args=server["extra_args"] or None,
            env=server["env"] or None,
            headers=server["headers"] or None,
            enabled=server["enabled"],
            tools=server["tools"] or None,
        )
        synced += 1

    return {"ok": True, "synced": synced, "removed": removed}


class McpReplaceRequest(BaseModel):
    mcpServers: dict[str, Any]


@app.put("/mcp/servers")
async def mcp_replace_servers(req: McpReplaceRequest):
    return _reconcile_mcp_servers(req.mcpServers)


@app.post("/mcp/sync")
async def mcp_sync_from_frontend(request: Request):
    """
    Backward-compatible bulk MCP replace endpoint.
    """
    body = await request.json()
    return _reconcile_mcp_servers(body.get("mcpServers"))


# ---------------------------------------------------------------------------
# Outputs — list OmicsClaw analysis output directories
# ---------------------------------------------------------------------------

# File-type classification for the outputs API
_OUTPUT_FILE_TYPES: dict[str, str] = {
    ".md": "markdown",
    ".json": "json",
    ".csv": "csv",
    ".tsv": "tsv",
    ".h5ad": "data",
    ".h5": "data",
    ".loom": "data",
    ".rds": "data",
    ".pdf": "pdf",
    ".html": "html",
    ".ipynb": "notebook",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".svg": "image",
    ".gif": "image",
    ".webp": "image",
    ".txt": "text",
    ".log": "text",
}

_IMAGE_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def _parse_run_dir_name(name: str) -> tuple[str, str | None]:
    """
    Parse an output directory name like ``sc-preprocessing__20260405_145208__dbe3570a``
    into (skill_name, iso_timestamp_or_None).

    The naming convention is: {skill}__{method}__{YYYYMMDD_HHMMSS}__{suffix}
    but the number of ``__`` segments varies, so we use a best-effort approach.
    """
    parts = name.split("__")
    skill = parts[0].replace("_", "-") if parts else name

    # Try to find a segment that looks like a timestamp (YYYYMMDD_HHMMSS)
    timestamp_iso: str | None = None
    for i, part in enumerate(parts):
        # A timestamp segment is 15 chars: YYYYMMDD_HHMMSS
        if len(part) == 15 and part[8] == "_":
            try:
                dt = datetime.strptime(part, "%Y%m%d_%H%M%S")
                timestamp_iso = dt.isoformat()
                break
            except ValueError:
                pass
        # Also handle without separator: YYYYMMDDHHMMSS (14 chars)
        if len(part) == 14 and part.isdigit():
            try:
                dt = datetime.strptime(part, "%Y%m%d%H%M%S")
                timestamp_iso = dt.isoformat()
                break
            except ValueError:
                pass

    return skill, timestamp_iso


def _classify_file(file_path: Path) -> str:
    """Return a type string for a file based on its extension."""
    return _OUTPUT_FILE_TYPES.get(file_path.suffix.lower(), "other")


def _collect_figures(
    run_dir: Path,
    *,
    claim_identities: frozenset[tuple[int, int]] | None = None,
) -> list[dict]:
    """Collect image files from the ``figures/`` subdirectory."""
    figures_dir = run_dir / "figures"
    if not figures_dir.is_dir():
        return []

    identities = (
        collect_output_claim_identities(run_dir)
        if claim_identities is None
        else claim_identities
    )
    results: list[dict] = []
    for f in sorted(figures_dir.iterdir()):
        if not is_scientific_output_file(
            f,
            output_root=run_dir,
            claim_identities=identities,
        ):
            continue
        ext = f.suffix.lower()
        mime = _IMAGE_MIME_MAP.get(ext)
        if mime:
            results.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "mimeType": mime,
                }
            )
    return results


def _collect_key_files(
    run_dir: Path,
    *,
    claim_identities: frozenset[tuple[int, int]] | None = None,
) -> list[dict]:
    """
    Collect notable files from the run directory (non-recursive, top-level only).

    Includes: report.md, result.json, README.md, *.csv, *.h5ad, and similar.
    """
    KEY_PATTERNS = {
        "report.md",
        "result.json",
        "readme.md",
        "result_summary.md",
        "completion_report.json",
    }
    KEY_EXTENSIONS = {
        ".csv",
        ".tsv",
        ".h5ad",
        ".h5",
        ".loom",
        ".rds",
        ".pdf",
        ".html",
        ".ipynb",
    }

    identities = (
        collect_output_claim_identities(run_dir)
        if claim_identities is None
        else claim_identities
    )
    results: list[dict] = []
    for f in sorted(run_dir.iterdir()):
        if not is_scientific_output_file(
            f,
            output_root=run_dir,
            claim_identities=identities,
        ):
            continue
        if f.name.lower() in KEY_PATTERNS or f.suffix.lower() in KEY_EXTENSIONS:
            results.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                    "type": _classify_file(f),
                }
            )
    return results


def _latest_file_mtime(run_dir: Path) -> float:
    latest = 0.0
    saw_entry = False
    claim_identities = collect_output_claim_identities(run_dir)
    claim_marker = run_dir / OUTPUT_CLAIM_FILENAME
    try:
        claim_stat = claim_marker.stat()
        if (
            not claim_marker.is_symlink()
            and claim_marker.is_file()
            and claim_stat.st_nlink == 1
            and is_contained_output_path(claim_marker, output_root=run_dir)
        ):
            latest = claim_stat.st_mtime
    except OSError:
        pass
    try:
        for entry in run_dir.rglob("*"):
            saw_entry = True
            try:
                if entry.is_dir():
                    if (
                        not entry.is_symlink()
                        and is_contained_output_path(entry, output_root=run_dir)
                        and next(entry.iterdir(), None) is None
                    ):
                        latest = max(latest, entry.stat().st_mtime)
                    continue
                if not is_scientific_output_file(
                    entry,
                    output_root=run_dir,
                    claim_identities=claim_identities,
                ):
                    continue
                latest = max(latest, entry.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    if not saw_entry:
        try:
            latest = run_dir.stat().st_mtime
        except OSError:
            return 0.0
    return latest


def _read_result_json(
    run_dir: Path,
    *,
    claim_identities: frozenset[tuple[int, int]] | None = None,
) -> tuple[str, Any]:
    """
    Read ``result.json`` and extract status + summary.

    Returns (status, summary_or_None).
    """
    result_file = run_dir / "result.json"
    identities = (
        collect_output_claim_identities(run_dir)
        if claim_identities is None
        else claim_identities
    )
    if not is_scientific_output_file(
        result_file,
        output_root=run_dir,
        claim_identities=identities,
    ):
        # result.json is the completion contract. Without it, a run is either
        # still active or was interrupted before finalization.
        latest_mtime = _latest_file_mtime(run_dir)
        if time.time() - latest_mtime > _OUTPUT_RUNNING_STALE_SECONDS:
            return (
                "failed",
                f"stale incomplete run: no result.json and no output update for more than {_OUTPUT_RUNNING_STALE_SECONDS // 60} minutes",
            )
        return ("running", None)

    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
        status = data.get("status", "")
        # Infer status when not explicitly set
        if not status:
            if data.get("completed_at"):
                status = "completed"
            elif data.get("error"):
                status = "failed"
            else:
                status = "completed"
        summary = data.get("summary") or data.get("message")
        # Build a summary from common fields if no explicit summary
        if not summary:
            parts: list[str] = []
            for k in ("n_cells", "n_genes", "n_clusters", "n_samples", "n_features"):
                if k in data:
                    parts.append(f"{k}={data[k]}")
            if parts:
                summary = ", ".join(parts)
        return (str(status), summary)
    except Exception:
        return ("unknown", None)


# ── Conversation → Run linkage (the desktop output 看板 "本对话" scope) ──────
#
# The producing chat session is the authoritative source of the link, so we
# record it here (server-side) rather than have the desktop frontend reverse-
# engineer it from streamed tool-result media paths (which silently misses
# text-only runs and any run whose figures aren't surfaced). When a tool result
# resolves to a Run directory under OUTPUT_DIR, we drop a small sidecar next to
# ``result.json``; ``/outputs/latest`` reads it back per Run.
_SESSION_SIDECAR_NAME = ".omicsclaw_session.json"


def _path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _run_dir_for_output_path(path_str: str, output_dir: Path) -> Path | None:
    """Walk up an output file/dir path to the Run directory under OUTPUT_DIR
    (the ancestor whose name carries the ``__YYYYMMDD_HHMMSS__`` run stamp)."""
    if not path_str:
        return None
    try:
        p = Path(str(path_str))
    except (TypeError, ValueError):
        return None
    for ancestor in (p, *p.parents):
        if _parse_run_dir_name(ancestor.name)[1] is None:
            continue
        if ancestor.is_dir() and _path_is_under(ancestor, output_dir):
            return ancestor
    return None


# Only stamp a Run produced during *this* turn. The completion marker
# (result.json) is written at finalization, so its age is a reliable "fresh"
# signal — it stops a read/list tool that merely *references* an older Run from
# re-stamping it with the current session.
_RUN_STAMP_RECENCY_SECONDS = 900


def _coerce_result_mapping(display_output: Any) -> dict[str, Any] | None:
    """Parse a tool-result payload into a mapping. Accepts a dict directly, a
    JSON string, and a Python-repr string (``str(dict)``) — the desktop SSE
    path str()-ifies dict results, so the repr form is a realistic shape."""
    if isinstance(display_output, dict):
        return display_output
    if isinstance(display_output, str):
        for loader in (json.loads, ast.literal_eval):
            try:
                parsed = loader(display_output)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _is_fresh_run_leaf(run_dir: Path) -> bool:
    """A real, just-produced Run leaf: it carries the completion contract
    (``result.json``/``manifest.json``) written within this turn's window."""
    claim_identities = collect_output_claim_identities(run_dir)
    marker = run_dir / "result.json"
    if marker.is_symlink() or not is_scientific_output_file(
        marker,
        output_root=run_dir,
        claim_identities=claim_identities,
    ):
        marker = run_dir / "manifest.json"
        if marker.is_symlink() or not is_scientific_output_file(
            marker,
            output_root=run_dir,
            claim_identities=claim_identities,
        ):
            return False
    try:
        return (time.time() - marker.stat().st_mtime) <= _RUN_STAMP_RECENCY_SECONDS
    except OSError:
        return False


def _resolve_session_run_dir(
    display_output: Any, media: list[dict[str, Any]], output_dir: Path
) -> Path | None:
    """Find the Run directory THIS tool result PRODUCED, or None. Trusts only
    producer signals — an explicit ``output_dir``/``output_directory``/``run_dir``
    field in the result payload, or a transferred media path — never arbitrary
    path strings (which may reference a *previous* run). The resolved directory
    must be a real, freshly-finalized Run leaf under OUTPUT_DIR."""
    candidates: list[str] = []
    parsed = _coerce_result_mapping(display_output)
    if parsed is not None:
        for key in ("output_dir", "output_directory", "run_dir"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                candidates.append(val)
    for item in media:
        if isinstance(item, dict):
            # A transferred media path, or the output_summary's run-dir anchor
            # (the only producer signal a figure-suppressed run carries).
            lp = (
                item.get("localPath")
                or item.get("path")
                or item.get("runDir")
                or item.get("run_dir")
            )
            if isinstance(lp, str) and lp:
                candidates.append(lp)
    for cand in candidates:
        run_dir = _run_dir_for_output_path(cand, output_dir)
        if run_dir is not None and _is_fresh_run_leaf(run_dir):
            return run_dir
    return None


def _write_session_sidecar(run_dir: Path, session_id: str) -> None:
    if not session_id:
        return
    sidecar = run_dir / _SESSION_SIDECAR_NAME
    try:
        atomic_write_owned_output_text(
            sidecar,
            output_root=run_dir,
            text=json.dumps(
                {
                    "session_id": session_id,
                    "stamped_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            ),
            label="desktop session sidecar",
        )
    except (OSError, RuntimeError):
        pass  # never let linkage bookkeeping break a run


def _read_session_sidecar(run_dir: Path) -> str:
    sidecar = run_dir / _SESSION_SIDECAR_NAME
    if not is_scientific_output_file(sidecar, output_root=run_dir):
        return ""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
        sid = data.get("session_id")
        return str(sid) if isinstance(sid, str) else ""
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def _stamp_session_for_run(
    session_id: str, display_output: Any, media: list[dict[str, Any]]
) -> None:
    """Record the producing chat session into the Run directory, if one is
    resolvable. Best-effort and exception-safe — called from the SSE loop."""
    if not session_id:
        return
    core = _get_core()
    output_dir = Path(str(core.OUTPUT_DIR))
    run_dir = _resolve_session_run_dir(display_output, media, output_dir)
    if run_dir is not None:
        _write_session_sidecar(run_dir, session_id)


@app.get("/outputs/latest")
async def outputs_latest(
    limit: int = Query(
        10, ge=1, le=200
    ),  # E-(4): 50→200 so the dashboard can page past the old cap
    project: str = "",  # plain default (not Query): unit tests call this fn directly
):
    """
    List the most recent Runs from OmicsClaw's OUTPUT_DIR.

    ADR 0035: Runs live under ``<OUTPUT_DIR>/<project>/<run>/`` (a legacy
    root-level run dir is tolerated as ``default``). Each entry carries its
    project, plus parsed metadata, figures, key files, and a ``result.json``
    summary when available. Pass ``?project=`` to scope to one project.
    """
    from omicsclaw.common import run_paths

    core = _get_core()
    output_dir = Path(str(core.OUTPUT_DIR))

    if not output_dir.is_dir():
        return {"runs": [], "output_dir": str(output_dir), "total": 0}

    # Walk one Project level (constraint 4); cache project_meta per project dir.
    meta_cache: dict[Path, dict[str, Any]] = {}
    dir_entries: list[
        tuple[float, Path, str, str]
    ] = []  # (mtime, run_dir, project_id, project_name)
    try:
        for project_dir, run_dir in run_paths.iter_run_dirs(output_dir):
            if (
                project_dir.is_symlink()
                or run_dir.is_symlink()
                or not is_contained_output_path(
                    project_dir,
                    output_root=output_dir,
                )
                or not is_contained_output_path(
                    run_dir,
                    output_root=output_dir,
                )
            ):
                continue
            meta = meta_cache.get(project_dir)
            if meta is None:
                meta = run_paths.read_project_meta(project_dir)
                meta_cache[project_dir] = meta
            project_id = str(
                meta.get("project_id")
                or (
                    run_paths.DEFAULT_PROJECT_ID
                    if project_dir == output_dir
                    else project_dir.name
                )
            )
            project_name = str(meta.get("display_name") or project_id)
            if project and project not in (project_id, project_dir.name):
                continue
            try:
                dir_entries.append(
                    (run_dir.stat().st_mtime, run_dir, project_id, project_name)
                )
            except OSError:
                pass
    except OSError as exc:
        raise HTTPException(500, detail=f"Cannot read output directory: {exc}")

    total = len(dir_entries)

    # Sort by mtime descending (newest first) and apply limit
    dir_entries.sort(key=lambda x: x[0], reverse=True)
    dir_entries = dir_entries[:limit]

    runs: list[dict] = []
    for mtime, d, project_id, project_name in dir_entries:
        skill, timestamp_iso = _parse_run_dir_name(d.name)
        claim_identities = collect_output_claim_identities(d)
        status, summary = _read_result_json(d, claim_identities=claim_identities)
        figures = _collect_figures(d, claim_identities=claim_identities)
        files = _collect_key_files(d, claim_identities=claim_identities)

        # Use mtime as the authoritative timestamp (always timezone-correct).
        # Fall back to the parsed directory-name timestamp only if mtime fails.
        effective_timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        if not effective_timestamp and timestamp_iso:
            effective_timestamp = timestamp_iso

        run_entry: dict[str, Any] = {
            "id": d.name,
            "skill": skill,
            "timestamp": effective_timestamp,
            "status": status,
            "path": str(d),
            "project_id": project_id,
            "project_name": project_name,
            "figures": figures,
            "files": files,
        }
        if summary:
            run_entry["summary"] = summary
        # Conversation linkage (本对话 scope): the session that produced this Run,
        # if the server recorded it (see _stamp_session_for_run). Empty otherwise.
        session_id = _read_session_sidecar(d)
        if session_id:
            run_entry["session_id"] = session_id

        runs.append(run_entry)

    return {"runs": runs, "output_dir": str(output_dir), "total": total}


@app.get("/outputs/{run_id}/files")
async def outputs_run_files(run_id: str):
    """
    List all files in a specific output directory (recursive).

    Returns a flat list of every file with path, size, and type classification.
    """
    from omicsclaw.common import run_paths

    core = _get_core()
    output_dir = Path(str(core.OUTPUT_DIR))

    # ADR 0035 constraint 4: resolve run_id -> path via a project-aware lookup,
    # never ``output_dir / run_id`` (Runs now nest under a project directory).
    # ``find_run_dir`` already rejects traversal and symlinked candidates.
    run_dir = run_paths.find_run_dir(output_dir, run_id)
    if run_dir is None or not run_dir.is_dir():
        raise HTTPException(404, detail=f"Output run '{run_id}' not found")

    files: list[dict] = []
    try:
        claim_identities = collect_output_claim_identities(run_dir)
        for f in sorted(run_dir.rglob("*")):
            if not is_scientific_output_file(
                f,
                output_root=run_dir,
                claim_identities=claim_identities,
            ):
                continue
            rel = f.relative_to(run_dir)
            mime, _ = mimetypes.guess_type(str(f))
            files.append(
                {
                    "name": f.name,
                    "relative_path": str(rel),
                    "path": str(f),
                    "size": f.stat().st_size,
                    "type": _classify_file(f),
                    "mimeType": mime or "application/octet-stream",
                }
            )
    except OSError as exc:
        raise HTTPException(500, detail=f"Cannot read run directory: {exc}")

    return {
        "run_id": run_id,
        "path": str(run_dir),
        "files": files,
        "total": len(files),
    }


# ---------------------------------------------------------------------------
# Bridge — Remote Channel Management
# ---------------------------------------------------------------------------

# Global ChannelManager instance (like _core for the LLM engine)
_channel_manager = None
_bridge_task: asyncio.Task | None = None

# Map each channel name to the env vars required for it to be considered
# "configured". If ALL listed vars are non-empty the channel is configured.
_CHANNEL_ENV_KEYS: dict[str, list[str]] = {
    "telegram": ["TELEGRAM_BOT_TOKEN"],
    "feishu": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    "dingtalk": ["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
    "discord": ["DISCORD_BOT_TOKEN"],
    "slack": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
    "wechat": ["WECOM_CORP_ID"],  # or WECHAT_APP_ID — either backend
    "qq": ["QQ_APP_ID", "QQ_APP_SECRET"],
    "email": [
        "EMAIL_IMAP_HOST",
        "EMAIL_IMAP_USERNAME",
        "EMAIL_SMTP_HOST",
        "EMAIL_SMTP_USERNAME",
    ],
    "imessage": [],  # macOS only, no secrets required
}

# Extended map: every env key relevant to a channel (for config endpoints).
_CHANNEL_ALL_CONFIG_KEYS: dict[str, list[str]] = {
    "telegram": [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "RATE_LIMIT_PER_HOUR",
    ],
    "feishu": [
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_THINKING_THRESHOLD_MS",
        "FEISHU_MAX_INBOUND_IMAGE_MB",
        "FEISHU_MAX_INBOUND_FILE_MB",
        "FEISHU_MAX_ATTACHMENTS",
        "FEISHU_RATE_LIMIT_PER_HOUR",
        "FEISHU_BRIDGE_DEBUG",
    ],
    "dingtalk": [
        "DINGTALK_CLIENT_ID",
        "DINGTALK_CLIENT_SECRET",
        "DINGTALK_RATE_LIMIT_PER_HOUR",
    ],
    "discord": [
        "DISCORD_BOT_TOKEN",
        "DISCORD_RATE_LIMIT_PER_HOUR",
        "DISCORD_PROXY",
    ],
    "slack": [
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_RATE_LIMIT_PER_HOUR",
    ],
    "wechat": [
        "WECOM_CORP_ID",
        "WECOM_AGENT_ID",
        "WECOM_SECRET",
        "WECOM_TOKEN",
        "WECOM_ENCODING_AES_KEY",
        "WECOM_WEBHOOK_PORT",
        "WECHAT_APP_ID",
        "WECHAT_APP_SECRET",
        "WECHAT_TOKEN",
        "WECHAT_ENCODING_AES_KEY",
        "WECHAT_WEBHOOK_PORT",
    ],
    "qq": [
        "QQ_APP_ID",
        "QQ_APP_SECRET",
        "QQ_ALLOWED_SENDERS",
        "QQ_RATE_LIMIT_PER_HOUR",
    ],
    "email": [
        "EMAIL_IMAP_HOST",
        "EMAIL_IMAP_PORT",
        "EMAIL_IMAP_USERNAME",
        "EMAIL_IMAP_PASSWORD",
        "EMAIL_IMAP_MAILBOX",
        "EMAIL_IMAP_USE_SSL",
        "EMAIL_SMTP_HOST",
        "EMAIL_SMTP_PORT",
        "EMAIL_SMTP_USERNAME",
        "EMAIL_SMTP_PASSWORD",
        "EMAIL_SMTP_STARTTLS",
        "EMAIL_FROM_ADDRESS",
        "EMAIL_POLL_INTERVAL",
        "EMAIL_MARK_SEEN",
        "EMAIL_ALLOWED_SENDERS",
    ],
    "imessage": [
        "IMESSAGE_CLI_PATH",
        "IMESSAGE_SERVICE",
        "IMESSAGE_REGION",
        "IMESSAGE_ALLOWED_SENDERS",
    ],
}

# Keys whose values must be masked in config responses
_SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY", "_KEY")


def _is_channel_configured(name: str) -> bool:
    """Check whether a channel's required env vars are all set."""
    required = _CHANNEL_ENV_KEYS.get(name, [])
    if not required:
        # wechat has two backends — check either
        if name == "wechat":
            return bool(
                os.environ.get("WECOM_CORP_ID") or os.environ.get("WECHAT_APP_ID")
            )
        # imessage: always "configured" on macOS, never on other platforms
        if name == "imessage":
            return sys.platform == "darwin"
        return False
    return all(os.environ.get(k) for k in required)


def _mask_value(key: str, value: str) -> str:
    """Mask secret values, showing only the last 4 chars."""
    if not value:
        return ""
    if any(key.upper().endswith(s) for s in _SECRET_SUFFIXES):
        if len(value) <= 4:
            return "****"
        return "*" * (len(value) - 4) + value[-4:]
    return value


def _get_omicsclaw_env_path() -> Path:
    """Return the path to the OmicsClaw project .env file."""
    omicsclaw_dir = os.getenv("OMICSCLAW_DIR", "")
    if omicsclaw_dir:
        return Path(omicsclaw_dir).resolve() / ".env"
    # Fallback: try parent of the omicsclaw package location
    try:
        import omicsclaw

        return Path(omicsclaw.__file__).resolve().parent.parent / ".env"
    except Exception:
        return Path.cwd() / ".env"


def _update_env_file(
    env_path: Path,
    updates: dict[str, str],
    *,
    remove_keys: set[str] | None = None,
) -> None:
    """Update key=value pairs in a .env file, preserving comments and order."""
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    effective_remove_keys = {
        str(key).strip() for key in (remove_keys or set()) if str(key).strip()
    }
    effective_remove_keys.difference_update(updates.keys())

    updated_keys: set[str] = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        raw_key = stripped.split("=", 1)[0].strip()
        if raw_key.startswith("export "):
            raw_key = raw_key[7:].strip()
        if raw_key in effective_remove_keys:
            continue
        if raw_key in updates:
            new_lines.append(f"{raw_key}={updates[raw_key]}")
            updated_keys.add(raw_key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    for key, value in updates.items():
        os.environ[key] = str(value)
    for key in effective_remove_keys:
        os.environ.pop(key, None)

    logger.info(
        "Updated %d key(s) and removed %d key(s) in %s",
        len(updates),
        len(effective_remove_keys),
        env_path,
    )


def _apply_chat_provider_switch(core: Any, provider_id: str, model: str) -> None:
    """Re-init ``core`` for a chat-initiated provider change — **runtime only**.

    Each chat carries its own ``provider_id``/``model`` (the desktop client
    binds the selection per session), so a per-message switch must be an
    *ephemeral* runtime override of the single global ``core`` client: it
    re-inits the LLM client for this turn but **must not** touch ``.env``.
    The persistent default provider is owned exclusively by the explicit
    ``PUT /providers`` Settings action; if a per-chat switch wrote ``.env``
    here, the global default would ping-pong to whichever provider the user
    last chatted with, breaking simultaneous multi-provider use.

    The chat request path has no ``auth_mode`` field, so ``core.init`` here
    always lands in ``api_key`` mode (credentials are read from the existing
    per-provider env keys).

    Raises the original ``core.init`` exception on failure — the caller must
    surface it to the user. Silently falling back to the previous provider
    would let the chat run against the old model while the UI reports the
    requested one.
    """
    _validate_custom_provider_input(
        provider=provider_id,
        model=model,
        base_url="",
        allow_env_base_url=True,
    )
    base_url = (
        _read_first_env("LLM_BASE_URL", "OMICSCLAW_BASE_URL")
        if str(provider_id or "").strip().lower() == "custom"
        else ""
    )
    core.init(provider=provider_id, model=model, base_url=base_url or None)

    logger.info(
        "Per-chat provider override (runtime-only, .env unchanged): provider=%s model=%s",
        getattr(core, "LLM_PROVIDER_NAME", provider_id),
        getattr(core, "OMICSCLAW_MODEL", ""),
    )


def _chat_request_requires_provider_reinit(
    core: Any, provider_id: str, model: str
) -> bool:
    requested_provider = str(provider_id or "").strip()
    if not requested_provider:
        return False

    current_provider = str(getattr(core, "LLM_PROVIDER_NAME", "") or "").strip()
    if requested_provider.lower() != current_provider.lower():
        return True

    requested_model = str(model or "").strip()
    if not requested_model:
        return False

    current_model = str(getattr(core, "OMICSCLAW_MODEL", "") or "").strip()
    return requested_model != current_model


# ---- Pydantic models for bridge endpoints --------------------------------


class BridgeStartRequest(BaseModel):
    channels: list[str]


class BridgeStopRequest(BaseModel):
    channels: list[str] = []  # empty = stop all


class BridgeConfigUpdateRequest(BaseModel):
    """Key-value pairs to write into the .env file."""

    # Accept arbitrary env var names as keys
    model_config = {"extra": "allow"}


# ---- GET /bridge/channels ------------------------------------------------


@app.get("/bridge/channels")
async def bridge_list_channels():
    """List all 9 available channels with configuration and running status."""
    global _channel_manager

    try:
        from omicsclaw.surfaces.channels import CHANNEL_REGISTRY
    except ImportError:
        raise HTTPException(503, detail="OmicsClaw channel system not available")

    channels = []
    for name in sorted(CHANNEL_REGISTRY.keys()):
        module_path, class_name = CHANNEL_REGISTRY[name]

        # Check if currently running + health info
        running = False
        stats = {}
        last_error = ""
        if _channel_manager is not None:
            running = name in _channel_manager.running_channels()
            health_data = _channel_manager.get_health()
            ch_health = health_data.get("channel_health", {}).get(name, {})
            stats = {
                "total_inbound": ch_health.get("total_inbound", 0),
                "total_outbound": ch_health.get("total_outbound", 0),
                "total_errors": ch_health.get("total_errors", 0),
            }
            last_error = ch_health.get("last_error", "")

        channels.append(
            {
                "name": name,
                "class": class_name,
                "configured": _is_channel_configured(name),
                "running": running,
                "stats": stats,
                "last_error": last_error,
            }
        )

    return {"channels": channels, "total": len(channels)}


# ---- POST /bridge/start --------------------------------------------------


@app.post("/bridge/start")
async def bridge_start(req: BridgeStartRequest):
    """Start one or more channels via ChannelManager in a background task."""
    global _channel_manager, _bridge_task

    if not req.channels:
        raise HTTPException(400, detail="No channels specified")

    # Validate channel names
    try:
        from omicsclaw.surfaces.channels import CHANNEL_REGISTRY
    except ImportError:
        raise HTTPException(503, detail="OmicsClaw channel system not available")

    unknown = [c for c in req.channels if c not in CHANNEL_REGISTRY]
    if unknown:
        raise HTTPException(
            400,
            detail=f"Unknown channel(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(CHANNEL_REGISTRY))}",
        )

    try:
        from omicsclaw.surfaces.channels.__main__ import CHANNEL_BUILDERS
        from omicsclaw.surfaces.channels.manager import ChannelManager
    except ImportError as exc:
        raise HTTPException(503, detail=f"Cannot import channel system: {exc}")

    # Load the OmicsClaw .env so channel builders can read their config
    try:
        from omicsclaw.common.runtime_env import load_env_file

        env_path = _get_omicsclaw_env_path()
        if env_path.exists():
            load_env_file(env_path, override=False)
            logger.info("Loaded OmicsClaw env from %s", env_path)
    except ImportError:
        logger.warning("Could not import load_env_file — env vars may be missing")

    core = _get_core()

    # Skip channels that are already running
    already_running = (
        _channel_manager.running_channels() if _channel_manager is not None else []
    )
    to_start = [c for c in req.channels if c not in already_running]
    skipped = [c for c in req.channels if c in already_running]

    if not to_start:
        return {
            "ok": True,
            "started": already_running,
            "registered": [],
            "skipped": skipped,
            "errors": None,
        }

    # Build and register each requested channel
    errors = {}
    registered = []

    # If manager already exists, add channels to it; otherwise create a new one
    if _channel_manager is not None:
        manager = _channel_manager
        for name in to_start:
            if name not in CHANNEL_BUILDERS:
                errors[name] = f"No builder for channel '{name}'"
                continue
            try:
                channel = CHANNEL_BUILDERS[name]()
                manager.register(channel)
                await manager.start_channel(name)
                registered.append(name)
                logger.info("Hot-added channel: %s", name)
            except Exception as exc:
                errors[name] = str(exc)
                logger.error("Failed to hot-add channel '%s': %s", name, exc)
    else:
        manager = ChannelManager()
        for name in to_start:
            if name not in CHANNEL_BUILDERS:
                errors[name] = f"No builder for channel '{name}'"
                continue
            try:
                channel = CHANNEL_BUILDERS[name]()
                manager.register(channel)
                registered.append(name)
                logger.info("Built and registered channel: %s", name)
            except Exception as exc:
                errors[name] = str(exc)
                logger.error("Failed to build channel '%s': %s", name, exc)

        if not registered:
            raise HTTPException(
                500,
                detail=f"No channels could be started. Errors: {errors}",
            )

        _channel_manager = manager

        # Run manager in a background asyncio task (don't block FastAPI)
        async def _run_bridge():
            try:
                await manager.start_all()
                logger.info(
                    "Bridge started with %d channel(s): %s",
                    len(manager.running_channels()),
                    ", ".join(manager.running_channels()),
                )
                core.audit(
                    "bridge_start",
                    channels=manager.running_channels(),
                )
                await manager.run()
            except asyncio.CancelledError:
                logger.info("Bridge task cancelled")
            except Exception as exc:
                logger.exception("Bridge task error: %s", exc)
            finally:
                await manager.stop_all()
                logger.info("Bridge stopped")

        _bridge_task = asyncio.create_task(_run_bridge(), name="bridge-manager")

    # Give channels a moment to initialise before responding
    await asyncio.sleep(0.5)

    return {
        "ok": True,
        "started": manager.running_channels(),
        "registered": registered,
        "skipped": skipped if skipped else None,
        "errors": errors if errors else None,
    }


# ---- POST /bridge/stop ---------------------------------------------------


@app.post("/bridge/stop")
async def bridge_stop(req: BridgeStopRequest = BridgeStopRequest()):
    """Stop running channels.

    If ``req.channels`` is provided, stop only those channels (the
    manager keeps running for the remaining ones).  If empty, stop
    everything and tear down the manager.
    """
    global _channel_manager, _bridge_task

    if _channel_manager is None:
        return {"ok": True, "message": "No bridge running", "stopped": []}

    # --- Per-channel stop ---
    if req.channels:
        stopped: list[str] = []
        for name in req.channels:
            if name in _channel_manager.running_channels():
                try:
                    await _channel_manager.stop_channel(name)
                    stopped.append(name)
                except Exception as exc:
                    logger.warning("Error stopping channel '%s': %s", name, exc)
        # If no channels remain, tear down the manager entirely
        if (
            not _channel_manager.running_channels()
            and not _channel_manager.enabled_channels
        ):
            if _bridge_task and not _bridge_task.done():
                _bridge_task.cancel()
                try:
                    await _bridge_task
                except asyncio.CancelledError:
                    pass
            _channel_manager = None
            _bridge_task = None
        return {"ok": True, "stopped": stopped}

    # --- Stop all ---
    stopped_all = list(_channel_manager.running_channels())

    if _bridge_task and not _bridge_task.done():
        _bridge_task.cancel()
        try:
            await _bridge_task
        except asyncio.CancelledError:
            pass

    try:
        await _channel_manager.stop_all()
    except Exception as exc:
        logger.warning("Error during bridge stop_all: %s", exc)

    _channel_manager = None
    _bridge_task = None

    return {"ok": True, "stopped": stopped_all}


# ---- GET /bridge/status --------------------------------------------------


@app.get("/bridge/status")
async def bridge_status():
    """Return overall bridge status with per-channel health."""
    global _channel_manager

    if _channel_manager is None:
        return {
            "running": False,
            "active_channels": [],
            "health": {},
        }

    health = _channel_manager.get_health()
    return {
        "running": bool(_channel_manager.running_channels()),
        "active_channels": _channel_manager.running_channels(),
        "registered_channels": _channel_manager.enabled_channels,
        "health": health,
    }


# ---- GET /bridge/config/{channel} ----------------------------------------


@app.get("/bridge/config/{channel}")
async def bridge_get_config(channel: str):
    """Return the configuration keys for a specific channel (secrets masked)."""
    if channel not in _CHANNEL_ALL_CONFIG_KEYS:
        raise HTTPException(
            404,
            detail=f"Unknown channel: {channel}. "
            f"Available: {', '.join(sorted(_CHANNEL_ALL_CONFIG_KEYS))}",
        )

    keys = _CHANNEL_ALL_CONFIG_KEYS[channel]
    config = {}
    for key in keys:
        value = os.environ.get(key, "")
        config[key] = {
            "value": _mask_value(key, value),
            "set": bool(value),
        }

    return {
        "channel": channel,
        "configured": _is_channel_configured(channel),
        "config": config,
    }


# ---- PUT /bridge/config/{channel} ----------------------------------------


@app.put("/bridge/config/{channel}")
async def bridge_update_config(channel: str, request: Request):
    """Update .env file at the OmicsClaw project root with provided key-value pairs."""
    if channel not in _CHANNEL_ALL_CONFIG_KEYS:
        raise HTTPException(
            404,
            detail=f"Unknown channel: {channel}. "
            f"Available: {', '.join(sorted(_CHANNEL_ALL_CONFIG_KEYS))}",
        )

    body = await request.json()
    if not body or not isinstance(body, dict):
        raise HTTPException(
            400, detail="Request body must be a JSON object with key-value pairs"
        )

    # Validate that all keys belong to this channel
    allowed_keys = set(_CHANNEL_ALL_CONFIG_KEYS[channel])
    invalid_keys = [k for k in body.keys() if k not in allowed_keys]
    if invalid_keys:
        raise HTTPException(
            400,
            detail=f"Keys not valid for channel '{channel}': {', '.join(invalid_keys)}. "
            f"Allowed: {', '.join(sorted(allowed_keys))}",
        )

    env_path = _get_omicsclaw_env_path()

    # Read existing .env content
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    # Parse existing key=value pairs, preserving order and comments
    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        # Preserve comments and blank lines
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        # Parse KEY=VALUE (handle optional quotes, export prefix)
        eq_idx = stripped.find("=")
        if eq_idx < 0:
            new_lines.append(line)
            continue
        raw_key = stripped[:eq_idx].strip()
        if raw_key.startswith("export "):
            raw_key = raw_key[7:].strip()
        if raw_key in body:
            # Replace this line with the new value
            new_lines.append(f"{raw_key}={body[raw_key]}")
            updated_keys.add(raw_key)
        else:
            new_lines.append(line)

    # Append any keys that were not already in the file
    for key, value in body.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    # Write back
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # Also update os.environ so changes take effect immediately
    for key, value in body.items():
        os.environ[key] = str(value)

    logger.info(
        "Updated %d config key(s) for channel '%s' in %s",
        len(body),
        channel,
        env_path,
    )

    return {
        "ok": True,
        "channel": channel,
        "updated_keys": list(body.keys()),
        "env_path": str(env_path),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the OmicsClaw desktop/web API server"
    )
    parser.add_argument(
        "--host",
        default=os.getenv("OMICSCLAW_APP_HOST", DEFAULT_APP_API_HOST),
        help=f"Host to bind (default: {DEFAULT_APP_API_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("OMICSCLAW_APP_PORT", str(DEFAULT_APP_API_PORT))),
        help=f"Port to bind (default: {DEFAULT_APP_API_PORT})",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=os.getenv("OMICSCLAW_APP_RELOAD", "false").lower()
        in ("true", "1", "yes"),
        help="Enable uvicorn reload mode",
    )
    return parser.parse_args(argv)


def _is_local_app_bind_host(host: str) -> bool:
    value = str(host or "").strip()
    if not value:
        # Uvicorn treats an empty host as a wildcard bind.
        return False
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_app_server_security(host: str, auth_token: str) -> None:
    if _is_local_app_bind_host(host) or str(auth_token or "").strip():
        return
    raise SystemExit(
        "Refusing to expose the Desktop API on a non-local interface without "
        "OMICSCLAW_REMOTE_AUTH_TOKEN. Bind to 127.0.0.1/::1/localhost or set "
        "a bearer token."
    )


def main(argv: list[str] | None = None):
    """Run the server from command line."""
    try:
        import uvicorn
    except ImportError:
        print(
            "ERROR: uvicorn is not installed. "
            f"Install with: {_APP_SERVER_INSTALL_HINT}",
            file=sys.stderr,
        )
        print("Minimal alternative: pip install fastapi uvicorn", file=sys.stderr)
        raise SystemExit(1)

    args = _parse_args(argv)
    _validate_app_server_security(
        args.host,
        os.environ.get("OMICSCLAW_REMOTE_AUTH_TOKEN", ""),
    )
    os.environ["OMICSCLAW_APP_HOST"] = args.host
    os.environ["OMICSCLAW_APP_PORT"] = str(args.port)

    logger.info("Starting OmicsClaw app backend on %s:%d", args.host, args.port)
    uvicorn.run(
        "omicsclaw.surfaces.desktop.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
