"""Session-scoped stdio facade. Business state remains in shared managers."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import fcntl
import importlib
import json
import os
import pwd
import re
import resource
import socket
import stat
import sys
import uuid
from contextlib import contextmanager
from importlib.metadata import distributions
from pathlib import Path

from omicsclaw.entry import AppConfig, SandboxMode, attach_sessions, build_app, open_sandbox
from omicsclaw.entry.assembly import DeploymentCapabilities
from omicsclaw.entry.config import (
    AppConfigError,
    SkillEnvMode,
    _as_csv_tuple,
    _as_sandbox_backend,
    _as_sandbox_egress,
    _as_sandbox_mode,
    _from_env,
)
from omicsclaw.entry.desktop.chat_service import (
    COMPACT_COMMAND,
    admit_chat,
    prepare_chat_intent,
)
from omicsclaw.entry.desktop.event_service import ProjectionCoverage, replay_frames
from omicsclaw.entry.desktop.interactions import DesktopInteractions, answer_permission
from omicsclaw.entry.desktop.jobs_manager import JobsManager
from omicsclaw.entry.desktop.provider_probe import probe_provider
from omicsclaw.entry.desktop.service import DesktopService
from omicsclaw.entry.desktop.title import generate_title
from omicsclaw.entry.desktop.turn_observation import DesktopChatSSEBody
from omicsclaw.entry.desktop.turn_submission import (
    decode_chat_stream_request,
    ingress_identity,
)
from omicsclaw.entry.memory import memory_db_path
from omicsclaw.entry.session import SubmissionRefused
from omicsclaw.entry.shadow_review import review_with_provider
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEventLog
from omicsclaw.observability import Telemetry
from omicsclaw.provider import ModelLimits, ProviderConfig, ProviderDialect
from omicsclaw.tools.context import context_value

from .manifest import verified_source_metadata
from .environments import EnvironmentRegistry, REGISTRY_SCHEMA_VERSION
from .notebook_ipc import NotebookOwnerClient
from ..notebook.binding import SSHNotebookBinding
from omicsclaw.entry.notebook.service import NotebookError
from .model_bridge import ModelSnapshot, bridged_provider
from .protocol import MAX_FRAME_BYTES, JsonPeer, PublicRpcError, RpcError
from .science_deployment import verified_science_deployment
from .science_runner import RunSkillJobTool, ScienceJobRunner

PROTOCOL_VERSION = 1
COMPONENT_VERSION = "0.2.0"


def process_start_time() -> str:
    """Linux process generation plus boot identity; a PID alone is never a fence."""
    stat_text = Path("/proc/self/stat").read_text()
    # The comm field can include spaces or parentheses. Field 22 follows it.
    ticks = stat_text.rsplit(")", 1)[1].split()[19]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return f"{boot}:{ticks}"


class WorkspaceOwner:
    """A kernel-held lock fences workspace writers and releases on process exit."""

    def __init__(self, workspace: Path, instance_id: str):
        state = workspace / ".omicsclaw"
        if state.is_symlink():
            raise ValueError("controller state cannot be a symlink")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(
            state / "controller.owner", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            inode = os.fstat(self.fd)
            if (
                not stat.S_ISREG(inode.st_mode)
                or inode.st_nlink != 1
                or inode.st_uid != os.getuid()
            ):
                raise ValueError("unsafe controller owner inode")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(self.fd, 0o600)
            os.ftruncate(self.fd, 0)
            os.write(
                self.fd,
                json.dumps(
                    {
                        "instanceId": instance_id,
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "user": pwd.getpwuid(os.getuid()).pw_name,
                        "startTime": process_start_time(),
                        "nonce": uuid.uuid4().hex,
                    }
                ).encode(),
            )
            os.fsync(self.fd)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)


class Controller:
    def __init__(self, workspace: Path, peer: JsonPeer):
        self.workspace = workspace.resolve(strict=True)
        self.peer = peer
        self.instance_id = uuid.uuid4().hex
        self.epoch = uuid.uuid4().hex
        self.app = None
        self.production = False
        self.interactions = None
        self.jobs = None
        self.science = None
        self.environments = None
        self.notebook_client = None
        self.environment_guard = None
        self.maintenance_jobs_task = None
        self._configuring = False
        self.factory = None
        self.model_snapshot = None
        self.role_providers = {}
        self.factories = []
        self.entry_identity = contextvars.ContextVar(
            "controller_model_call", default=None
        )
        self.installation_id = ""
        self.observers: dict[str, asyncio.Task] = {}
        self.owner = WorkspaceOwner(self.workspace, self.instance_id)
        # Bootstrap the existing memory metadata under the workspace fence.
        # Close it before build_app opens its single business connection.
        database_path = memory_db_path(AppConfig(workspace=self.workspace))
        if database_path.is_symlink():
            self.owner.close()
            raise ValueError("controller memory state cannot be a symlink")
        try:
            database = Database(database_path)
        except BaseException:
            self.owner.close()
            raise
        try:
            self.identity = DurableTurnEventLog(database).identity
        finally:
            database.close()
        peer.handlers.update(
            {
                "hello": self.hello,
                "configure": self.configure,
                "chat.submit": self.submit,
                "chat.validate": self.validate_intent,
                "chat.permission": self.permission,
                "chat.abort": self.abort,
                "request.lookup": self.lookup,
                "requests.list": self.requests,
                "events.replay": self.replay,
                "events.frames": self.frames,
                "session.snapshot": self.session_snapshot,
                "sessions.list": self.sessions,
                "sessions.live": self.session_live,
                "runtime.imports": self.imports,
                "environments.list": self.environment_list,
                "environments.register": self.environment_register,
                "environments.preflight": self.environment_preflight,
                "environments.accept": self.environment_accept,
                "desktop.request": self.desktop_request,
                "model.title": self.title,
                "model.test": self.provider_test,
            }
        )
        for method in ("attach", "list", "bind", "admit", "run", "snapshot", "execution", "events", "cancel", "release", "binding", "receipt", "changes", "cell", "save", "resume", "selectEnvironment", "artifactInfo", "artifactChunk"):
            async def notebook_handler(params, method=method):
                return await self.notebook_request(method, params)
            peer.handlers["notebooks." + method] = notebook_handler

        for action in ("Plan", "Approve", "Interrupt", "Cancel", "Repair", "Restart", "Get", "List", "Impact", "Events", "Receipt"):
            async def maintenance_handler(params, action=action):
                return await self.maintenance_request(action.lower(), params)
            peer.handlers["environments.maintenance" + action] = maintenance_handler
        for method in ("analysisState", "analysisPage", "analysisCommand"):
            async def analysis_handler(params, method=method):
                return await self.analysis_request(method, params)
            peer.handlers["notebooks." + method] = analysis_handler

    async def analysis_request(self, method, params):
        self._app()
        if self.notebook_client is None:
            raise PublicRpcError("notebook_analysis_unavailable")
        read = method != "analysisCommand"
        field = "scope" if read else "target"
        base = self._environment_target()
        target = params.get(field)
        if read:
            if target != {key: base[key] for key in ("authorityId", "workspaceId")}:
                raise PublicRpcError("notebook_target_changed")
        elif not isinstance(target, dict) or any(target.get(key) != value for key, value in base.items()):
            raise PublicRpcError("notebook_target_changed")
        try:
            health = await self.notebook_client.attach()
            owner = {key: health["owner"][key] for key in ("ownerId", "ownerEpoch")}
            if not read and target != {**base, **owner}:
                raise PublicRpcError("notebook_target_changed")
            result = await self.notebook_client.call(
                {"analysisState": "analysis_state", "analysisPage": "analysis_page", "analysisCommand": "analysis_command"}[method],
                {key: value for key, value in params.items() if key != field}, expected_owner=owner)
            if not read:
                snapshot = await self.notebook_client.call("snapshot", {"notebookId": params["notebookId"], "limit": 1}, expected_owner=owner)
                if snapshot["binding"]:
                    for tool in ("python", "r", "notebook_analysis"):
                        self.interactions.invalidate_tool(snapshot["binding"]["session_id"], tool, "Notebook data state changed; prepare a new request after Resume.")
            return result
        except NotebookError as error:
            raise PublicRpcError(error.code) from error
        except (OSError, TimeoutError):
            raise PublicRpcError("notebook_analysis_unavailable") from None

    async def maintenance_request(self, action, params):
        self._app()
        if self.notebook_client is None:
            raise PublicRpcError("environment_maintenance_unavailable")
        base = self._environment_target()
        read = action in {"get", "list", "impact", "events", "receipt"}
        target = params.get("scope" if read else "target")
        if read:
            if target != {k: base[k] for k in ("authorityId", "workspaceId")}:
                raise PublicRpcError("environment_target_changed")
        elif not isinstance(target, dict) or any(target.get(k) != v for k, v in base.items()):
            raise PublicRpcError("environment_target_changed")
        try:
            health = await self.notebook_client.attach()
            owner = {k: health["owner"][k] for k in ("ownerId", "ownerEpoch")}
            if not read and target != {**base, **owner}:
                raise PublicRpcError("environment_target_changed")
            return await self.notebook_client.call("maintenance", {"action": action,
                "params": {k:v for k,v in params.items() if k != ("scope" if read else "target")}}, expected_owner=owner)
        except NotebookError as error:
            raise PublicRpcError(error.code) from error
        except (OSError, TimeoutError):
            raise PublicRpcError("environment_maintenance_unavailable") from None

    async def hello(self, params):
        if params.get("protocolVersion") != PROTOCOL_VERSION:
            raise RpcError("unsupported protocol")
        source_root = Path(__file__).resolve().parents[3]
        metadata = (
            verified_source_metadata(source_root)
            if (source_root / "controller-manifest.json").exists()
            else {"manifestVerified": False}
        )
        if metadata.get("manifestVerified") and (
            metadata["componentVersion"] != COMPONENT_VERSION
            or metadata["protocolVersion"] != PROTOCOL_VERSION
        ):
            raise RpcError("component manifest version mismatch")
        return {
            **metadata,
            "protocolVersion": PROTOCOL_VERSION,
            "componentVersion": COMPONENT_VERSION,
            "controllerInstanceId": self.instance_id,
            "epoch": self.epoch,
            "workspacePath": str(self.workspace),
            "workspaceId": self.identity.workspace_id,
            "authorityId": self.identity.authority_id,
            "durableIdentity": dataclasses.asdict(self.identity),
            "capabilities": [
                "base",
                "sdk_model_bridge",
                "durable_admission",
                "named_model_callers",
                "stable_event_subframes",
                "read_only_history",
                "external_science_binding",
                "registered_science_environments_v1",
                "notebook_owner_v1",
                "notebook_model_v1",
            ],
        }

    async def configure(self, params):
        if self.app is not None or self._configuring:
            raise RpcError("configuration is immutable for this connection")
        self._configuring = True
        try:
            return await self._configure(params)
        finally:
            self._configuring = False

    async def _configure(self, params):
        if self.app is not None:
            raise RpcError("configuration is immutable for this connection")
        roles = (
            "main",
            "summary",
            "subagent",
            "title",
            "shadow_review",
            "provider_test",
        )
        production = params.get("schemaVersion") == 2
        features = {"shadowReview": False}
        if production:
            if (
                set(params)
                - {
                    "schemaVersion",
                    "roles",
                    "installationId",
                    "features",
                    "science",
                    "sandbox",
                }
                or not {"schemaVersion", "roles", "installationId"} <= set(params)
                or not isinstance(params["roles"], dict)
                or set(params["roles"]) != set(roles)
            ):
                raise RpcError("all named model snapshots required")
            if not isinstance(params["installationId"], str) or not re.fullmatch(
                r"[0-9a-f]{32}", params["installationId"]
            ):
                raise RpcError("installation identity required")
            self.installation_id = params["installationId"]
            features = params.get("features", {"shadowReview": False})
            if (
                not isinstance(features, dict)
                or not {"shadowReview"} <= set(features)
                or set(features) - {"shadowReview", "sandbox"}
                or type(features["shadowReview"]) is not bool
            ):
                raise RpcError("invalid controller feature snapshot")
            snapshots = params["roles"]
        else:
            snapshots = {
                "main": {k: v for k, v in params.items() if k != "sandbox"}
                if isinstance(params, dict)
                else params
            }
        sandbox_kwargs = _resolve_sandbox_kwargs(
            params if isinstance(params, dict) else {},
            production=production,
            features=features,
        )
        science = None
        if production and params.get("science") is not None:
            science = await asyncio.to_thread(
                verified_science_deployment, params["science"]
            )
        permitted = {field.name for field in dataclasses.fields(ModelSnapshot)}
        parsed = {}
        for role, document in snapshots.items():
            if not isinstance(document, dict) or set(document) - permitted:
                raise RpcError("unknown model snapshot fields")
            parsed[role] = ModelSnapshot(
                **{
                    **document,
                    "dialect": ProviderDialect(document["dialect"]),
                    "allowed_models": tuple(document["allowed_models"]),
                }
            )
        for role, selected in parsed.items():
            provider, factory = bridged_provider(
                selected,
                self.peer,
                role=role if production else None,
                identity=self.model_call_identity if production else None,
            )
            self.role_providers[role] = provider
            self.factories.append(factory)
        snapshot = parsed["main"]
        provider = self.role_providers["main"]
        self.factory = self.factories[0]
        self.production = production
        self.snapshots = parsed

        async def review_call(session_id, records, history):
            return await review_with_provider(
                self.role_providers["shadow_review"].bind(
                    max_tokens=min(4096, parsed["shadow_review"].output_tokens)
                ),
                session_id,
                records,
                history,
            )

        config = AppConfig(
            workspace=self.workspace,
            provider=snapshot.provider_id,
            model=snapshot.model,
            durable_turn_events=True,
            shadow_review=features["shadowReview"] if production else False,
            skill_env=SkillEnvMode.OFF,
            skills_dir=(
                science.payload_root if science else Path(__file__).resolve().parents[3]
            )
            / "skills",
            **sandbox_kwargs,
        )
        from omicsclaw.entry.environment_runtime import ProcessEnvironmentGuard
        self.environment_guard = ProcessEnvironmentGuard(
            self.workspace, workspace_id=self.identity.workspace_id, role="ssh_controller"
        )
        sandbox = await open_sandbox(config)
        try:
            raw_sandbox_cfg = config.sandbox_config()
            code_mounts = set(config.code_mounts())
            owner_sandbox_cfg = (
                dataclasses.replace(
                    raw_sandbox_cfg,
                    extra_read_paths=tuple(
                        p for p in raw_sandbox_cfg.extra_read_paths if p not in code_mounts
                    ),
                    read_only_mounts=tuple(
                        p for p in raw_sandbox_cfg.read_only_mounts if p not in code_mounts
                    ),
                )
                if raw_sandbox_cfg is not None and code_mounts
                else raw_sandbox_cfg
            )
            self.notebook_client = NotebookOwnerClient(
                self.workspace,
                {"authorityId": self.identity.authority_id, "workspaceId": self.identity.workspace_id},
                self.instance_id, self.epoch,
                sandbox_config=owner_sandbox_cfg,
            )
            self.notebook_binding = SSHNotebookBinding(self.notebook_client)
            assembled = build_app(
                config,
                additional_tools=(RunSkillJobTool(lambda: self.jobs),) if science else (),
                provider=provider,
                resolved_model=snapshot.model,
                model_limits=ModelLimits(snapshot.context_tokens, snapshot.output_tokens),
                capabilities=DeploymentCapabilities(notebook=True),
                telemetry=Telemetry(),
                provider_for_role=(self.role_providers.__getitem__ if production else None),
                review_call=review_call if production else None,
                kernel_binding=self.notebook_binding,
                sandbox=sandbox,
            )
        except BaseException:
            await sandbox.aclose()
            raise
        bridge = None
        try:
            if science:
                from omicsclaw.remote import RemoteJobsBridge, RemotePlaneBinding
                from omicsclaw.remote.local import LocalTargetSpawner

                spawner = LocalTargetSpawner(self.workspace)
                plane = RemotePlaneBinding(
                    Database(self.workspace / ".omicsclaw/remote.db"),
                    workspace=self.workspace,
                    spawner=spawner,
                )
                assembled = dataclasses.replace(assembled, remote=plane)
                bridge = RemoteJobsBridge(
                    plane, target_alias="controller", target_root=str(self.workspace)
                )
            self.app = attach_sessions(assembled, abandon_grace_s=0)
        except BaseException:
            await assembled.aclose()
            raise
        if self.app.sessions.durable_identity is None:
            await self.app.aclose()
            self.app = None
            raise RpcError("durable admission capability required")
        if self.app.sessions.durable_identity != self.identity:
            await self.app.aclose()
            self.app = None
            raise RpcError("workspace authority identity changed")
        self.interactions = DesktopInteractions(self.app)
        self.jobs = JobsManager(
            self.app, remote_runtime=bridge, register_default_runners=False
        )
        if science:
            from omicsclaw.entry.environment_jobs import ScienceEnvironmentGuard
            self.jobs.register_runner(
                "skill_run",
                ScienceJobRunner(
                    self.app,
                    bridge,
                    interpreter=science.python,
                    component_root=Path(__file__).resolve().parents[3],
                    payload_root=science.payload_root,
                    manifest=science.payload_manifest,
                    environment_digest=science.environment_digest,
                    default_hard_timeout_s=3600.0,
                    environment_guard=ScienceEnvironmentGuard(self.environment_guard,
                        interpreter=science.python, rscript=science.rscript, r_library=science.r_library),
                    rscript=science.rscript,
                    r_library=science.r_library,
                ),
            )
            self.science = science
        self.model_snapshot = snapshot
        from omicsclaw.entry.environment_reads import EnvironmentReadGuard
        self.environments = EnvironmentRegistry(
            self.app.memory.database, self.workspace,
            wrapper=self.app.sandbox.as_kernel_spawn_wrapper() if self.app.sandbox else None,
            read_guard=EnvironmentReadGuard(self.environment_guard.coordinator,
                self.workspace, self.identity.workspace_id, self.environment_guard.owner),
        )
        from omicsclaw.entry.maintenance_jobs import poll_job_interrupts
        self.maintenance_jobs_task = asyncio.create_task(poll_job_interrupts(self))
        if self.app.shadow_review:
            self.app.shadow_review.bind_store(self.jobs.artifacts)
        return {
            "configured": True,
            "revision": snapshot.revision,
            "durableIdentity": dataclasses.asdict(self.app.sessions.durable_identity),
        }

    def _app(self):
        if self.app is None:
            raise RpcError("configure required")
        return self.app

    async def submit(self, params):
        app = self._app()
        request = decode_chat_stream_request(params)
        if self.production and request.installation_id != self.installation_id:
            raise RpcError("installation identity mismatch")
        if not request.source_request_id:
            raise RpcError("source request identity required")
        if request.resume:
            raise RpcError("resume must observe durable events; it cannot submit")
        if request.workspace and Path(request.workspace).resolve() != self.workspace:
            raise RpcError("workspace mismatch")
        with self.model_entry(request.source_request_id, request.session_id):
            settled = await admit_chat(
                app,
                params,
                interactions=self.interactions,
                deployment_values=self.deployment_values(),
            )
        handle, newly_admitted = settled.handle, settled.newly_admitted
        if newly_admitted:
            task = asyncio.create_task(self._observe(handle))
            self.observers[handle.turn_id] = task
            task.add_done_callback(
                lambda done: self._observer_done(handle.turn_id, done)
            )
        return {
            "turnId": handle.turn_id,
            "sessionId": handle.session_id,
            "firstRequest": DurableTurnEventLog(app.memory.database).is_first_request(
                handle.session_id, request.to_inbound().source_request_id
            ),
        }

    def deployment_values(self):
        return (
            {
                "__desktop_model_snapshot__": {
                    "provider": self.model_snapshot.provider_id,
                    "model": self.model_snapshot.model,
                    "revision": self.model_snapshot.revision,
                }
            }
            if self.production
            else None
        )

    async def validate_intent(self, params):
        request, inbound = prepare_chat_intent(
            self._app(), params, deployment_values=self.deployment_values()
        )
        if (
            request.resume
            or not request.source_request_id
            or self.production
            and request.installation_id != self.installation_id
        ):
            raise RpcError("invalid intent validation identity")
        try:
            receipt = self._app().sessions.validate_admitted_intent(
                inbound, compaction_only=request.content.strip() == COMPACT_COMMAND
            )
        except SubmissionRefused:
            return {"valid": False, "code": "request_payload_conflict"}
        return {
            "valid": receipt is not None,
            "code": "request_not_admitted" if receipt is None else "",
            "turnId": receipt["turn_id"] if receipt else None,
        }

    def _observer_done(self, turn_id, task):
        if self.observers.get(turn_id) is task:
            self.observers.pop(turn_id, None)
        if not task.cancelled():
            task.exception()

    async def _observation_fault(self, handle, source_request_id, code):
        handle.cancel()
        # This diagnostic revokes observation, never declaring a business terminal.
        try:
            async with asyncio.timeout(1):
                await self.peer.send(
                    {
                        "kind": "event",
                        "method": "chat.fault",
                        "params": {
                            "sessionId": handle.session_id,
                            "turnId": handle.turn_id,
                            "sourceRequestId": source_request_id,
                            "epoch": self.epoch,
                            "code": code,
                        },
                    }
                )
        except (TimeoutError, RpcError, ConnectionError):
            pass
        try:
            async with asyncio.timeout(0.25):
                await handle.wait()
        except TimeoutError:
            pass

    async def _observe(self, handle):
        receipt = DurableTurnEventLog(self.app.memory.database).turn_receipt(
            handle.session_id, handle.turn_id
        )
        source_request_id = (
            self.caller_request_id(receipt["source_request_id"]) if receipt else ""
        )
        coverage = ProjectionCoverage(handle.observe())
        metadata = (receipt or {}).get("payload", {}).get("values", {}).get(
            "__desktop_model_snapshot__"
        ) or {}
        body = DesktopChatSSEBody(
            coverage,
            keepalive_s=None,
            approvals=handle.approvals,
            interactions=self.interactions,
            provider=metadata.get("provider", self.model_snapshot.provider_id),
            model=metadata.get("model", self.model_snapshot.model),
            compaction=handle if handle.compaction_only else None,
        )
        stable_frames, projection_bytes = [], 0
        try:
            async with body:
                async for frame in body:
                    # The shared projector owns approval registration and wire semantics.
                    if not await handle.stream.flush_durable():
                        raise RpcError("durable_turn_write_failed")
                    frame_type = json.loads(frame.split("data: ", 1)[1]).get("type")
                    if frame_type == "done" and (
                        coverage.terminal_seq <= 0
                        or coverage.terminal_seq != body.last_seq
                    ):
                        raise RpcError("durable_turn_terminal_missing")
                    stable = {
                        "seq": body.last_seq,
                        "subframe": body.last_subframe,
                        "frame": frame,
                    }
                    projection_bytes += len(DurableTurnEventLog._canonical(stable)) + 1
                    if (
                        projection_bytes > 4 * 1024 * 1024
                        or len(stable_frames) >= 20000
                    ):
                        raise RpcError("projection_requires_data_channel")
                    stable_frames.append(stable)
                    if (
                        frame_type == "done"
                        and receipt is not None
                        and coverage.continuous
                    ):
                        log = DurableTurnEventLog(self.app.memory.database)
                        source = log.projection_source(
                            handle.session_id, handle.turn_id
                        )
                        if (
                            source["rawDigest"] != coverage.digest.hexdigest()
                            or source["ownerGeneration"]
                            != handle.stream.durable_owner_generation
                        ):
                            raise RpcError("projection_source_changed")
                        log.confirm_projection(source, stable_frames)
                    await self.peer.send(
                        {
                            "kind": "event",
                            "method": "chat.frame",
                            "params": {
                                "turnId": handle.turn_id,
                                "sessionId": handle.session_id,
                                "sourceRequestId": source_request_id,
                                "cursor": body.last_seq,
                                "subframe": body.last_subframe,
                                "eventStreamId": json.dumps(
                                    [handle.session_id, handle.turn_id],
                                    separators=(",", ":"),
                                ),
                                "epoch": self.epoch,
                                "frame": frame,
                                "durableIdentity": dataclasses.asdict(
                                    self.app.sessions.durable_identity
                                ),
                            },
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = (
                str(error)
                if str(error)
                in {
                    "durable_turn_write_failed",
                    "durable_turn_terminal_missing",
                    "projection_requires_data_channel",
                    "projection_source_changed",
                }
                else "projection_confirmation_failed"
            )
            await self._observation_fault(handle, source_request_id, code)
            raise

    async def permission(self, params):
        return await answer_permission(self._app(), self.interactions, params)

    async def abort(self, params):
        handle = self._app().sessions.handle(params["turnId"])
        handle.cancel()
        return {"accepted": True}

    async def lookup(self, params):
        receipt = self._app().sessions.request_receipt(
            params["sessionId"], self.request_key(params["sourceRequestId"])
        )
        return {
            "receipt": receipt,
            "firstRequest": receipt is not None
            and DurableTurnEventLog(self._app().memory.database).is_first_request(
                params["sessionId"], receipt["source_request_id"]
            ),
        }

    async def requests(self, params):
        app = self._app()
        if params.get("descending") is True or params.get("direction") == "forward":
            return {
                **DurableTurnEventLog(app.memory.database).request_page(
                    params["sessionId"],
                    before=params.get("before"),
                    limit=params.get("limit", 50),
                    include_boundary=params.get("includeBoundary") is True,
                    forward=params.get("direction") == "forward",
                    through=params.get("through"),
                ),
                "durableIdentity": dataclasses.asdict(self.identity),
            }
        offset = params.get("offset", 0)
        limit = params.get("limit", 50)
        records = DurableTurnEventLog(app.memory.database).requests(
            params["sessionId"], offset=offset, limit=limit
        )
        return {
            "requests": records,
            "nextOffset": offset + len(records),
            "hasMore": bool(records),
            "durableIdentity": dataclasses.asdict(self.identity),
        }

    async def replay(self, params):
        app = self._app()
        if not app.memory:
            raise RpcError("durable state required")
        session_id, turn_id = params["sessionId"], params["turnId"]
        log = DurableTurnEventLog(app.memory.database)
        stream_id = json.dumps([session_id, turn_id], separators=(",", ":"))
        events = log.replay(stream_id, after_seq=int(params.get("afterSeq", 0)))
        return {"events": [dataclasses.asdict(event) for event in events]}

    async def desktop_request(self, params):
        return await DesktopService(self._app(), self.jobs, self.interactions).dispatch(
            params
        )

    async def frames(self, params):
        return await replay_frames(
            self._app(),
            params["sessionId"],
            params["turnId"],
            after_seq=params.get("afterSeq", 0),
            after_subframe=params.get("afterSubframe", -1),
            epoch=self.epoch,
            limit=params.get("limit", 64),
            interactions=self.interactions,
        )

    async def session_snapshot(self, params):
        app = self._app()
        if "turnId" in params:
            page = DurableTurnEventLog(app.memory.database).projection_page(
                params["sessionId"],
                params["turnId"],
                cursor=params.get("cursor"),
                limit=params.get("limit", 64),
                frame_overhead=1024,
            )
            return {
                **page,
                "frames": [
                    {
                        "durableIdentity": dataclasses.asdict(self.identity),
                        "epoch": self.epoch,
                        "eventStreamId": json.dumps(
                            [params["sessionId"], params["turnId"]],
                            separators=(",", ":"),
                        ),
                        "sessionId": params["sessionId"],
                        "turnId": params["turnId"],
                        "cursor": frame["seq"],
                        "subframe": frame["subframe"],
                        "frame": frame["frame"],
                    }
                    for frame in page["frames"]
                ],
            }
        session = await app.sessions.load_session(params["sessionId"])
        if session is None:
            return {"session": None}
        offset, limit = params.get("offset", 0), params.get("limit", 50)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise RpcError("invalid history page")
        messages = []
        size = 0
        for position, message in enumerate(
            session.history[offset : offset + limit], start=offset
        ):
            item = {
                "position": position,
                "role": message.role.value,
                "content": message.content,
                "reasoning": message.reasoning_content,
                "tool_calls": [
                    dataclasses.asdict(call) for call in message.tool_calls or ()
                ],
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "is_error": message.is_error,
            }
            cost = len(json.dumps(item).encode())
            if cost > 524288:
                raise RpcError("history_message_requires_data_channel")
            if size + cost > 524288:
                break
            messages.append(item)
            size += cost
        return {
            "session": {
                "sessionId": session.session_id,
                "createdAt": session.created_at,
                "updatedAt": session.updated_at,
            },
            "messages": messages,
            "nextOffset": offset + len(messages),
            "hasMore": offset + len(messages) < len(session.history),
            "durableIdentity": dataclasses.asdict(self.identity),
        }

    async def session_live(self, params):
        app = self._app()
        session_id = params["sessionId"]
        live = [
            handle
            for handle in app.sessions.running()
            if handle.session_id == session_id
        ]
        if not live:
            return {"active": False, "source_request_id": None}
        receipt = DurableTurnEventLog(app.memory.database).turn_receipt(
            session_id, live[0].turn_id
        )
        return {
            "active": True,
            "turn_id": live[0].turn_id,
            "source_request_id": (
                self.caller_request_id(receipt["source_request_id"])
                if receipt
                else None
            ),
        }

    async def sessions(self, params):
        app = self._app()
        rows = {
            row.session_id: {
                "sessionId": row.session_id,
                "updatedAt": row.updated_at,
                "createdAt": row.created_at,
            }
            for row in await app.sessions.list_sessions(limit=100)
        }
        for row in DurableTurnEventLog(app.memory.database).admitted_sessions():
            rows.setdefault(
                row["session_id"],
                {
                    "sessionId": row["session_id"],
                    "updatedAt": row["updated_at"],
                    "createdAt": row["created_at"],
                },
            )
        return {
            "sessions": sorted(
                rows.values(), key=lambda row: row["updatedAt"], reverse=True
            )[:100]
        }

    def model_call_identity(self):
        session_id = context_value("session_id")
        turn_id = context_value("turn_id")
        if session_id and turn_id and self.app is not None:
            receipt = DurableTurnEventLog(self.app.memory.database).turn_receipt(
                session_id, turn_id
            )
            if receipt:
                return {
                    "controllerInstanceId": self.instance_id,
                    "epoch": self.epoch,
                    "requestId": self.caller_request_id(receipt["source_request_id"]),
                    "sessionId": session_id,
                    "turnId": turn_id,
                }
            raise RpcError("model turn has no durable admission")
        identity = self.entry_identity.get()
        if identity and not identity["turnId"] and self.app is not None:
            receipt = self.app.sessions.request_receipt(
                identity["sessionId"], self.request_key(identity["requestId"])
            )
            if receipt:
                return {**identity, "turnId": receipt["turn_id"]}
        return identity

    def request_key(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            raise RpcError("invalid source identity")
        return f"{ingress_identity(self.installation_id)[2]}/{request_id}"

    def caller_request_id(self, request_key):
        namespace = ingress_identity(self.installation_id)[2] + "/"
        if not isinstance(request_key, str) or not request_key.startswith(namespace):
            raise RpcError("model receipt installation mismatch")
        request_id = request_key[len(namespace) :]
        if self.request_key(request_id) != request_key:
            raise RpcError("invalid model receipt identity")
        return request_id

    @contextmanager
    def model_entry(self, request_id, session_id, turn_id=""):
        if (
            not isinstance(request_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", request_id)
            or not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 128
            or not isinstance(turn_id, str)
            or turn_id
            and not re.fullmatch(r"[0-9a-f]{32}", turn_id)
        ):
            raise RpcError("model caller identity required")
        token = self.entry_identity.set(
            {
                "controllerInstanceId": self.instance_id,
                "epoch": self.epoch,
                "requestId": request_id,
                "sessionId": session_id,
                "turnId": turn_id,
            }
        )
        try:
            yield
        finally:
            self.entry_identity.reset(token)

    async def title(self, params):
        app = self._app()
        receipt = DurableTurnEventLog(app.memory.database).turn_receipt(
            params.get("sessionId"), params.get("turnId")
        )
        if receipt is None:
            raise RpcError("title parent turn not admitted")
        self.caller_request_id(receipt["source_request_id"])
        document = params["document"]
        with self.model_entry(
            document.get("source_request_id"),
            params.get("sessionId"),
            params.get("turnId", ""),
        ):
            status, result = await generate_title(
                app,
                document,
                max_tokens=min(
                    1024, self.snapshots.get("title", self.model_snapshot).output_tokens
                ),
            )
        return {"status": status, "payload": result}

    async def provider_test(self, params):
        self._app()
        if (
            set(params) != {"requestId", "sessionId"}
            or params["sessionId"] != "provider-test"
        ):
            raise RpcError("invalid provider test scope")
        if not self.production:
            raise RpcError("named model configuration required")
        snapshot = self.snapshots["provider_test"]
        config = ProviderConfig(
            provider=snapshot.provider_name,
            model=snapshot.model,
            api_key="ssh-bridge-placeholder",
            max_tokens=min(256, snapshot.output_tokens),
            temperature=0,
        )
        with self.model_entry(params.get("requestId"), params.get("sessionId")):
            result = await probe_provider(
                config,
                provider_factory=lambda selected: self.role_providers[
                    "provider_test"
                ].bind(
                    max_tokens=selected.max_tokens,
                    temperature=0,
                    timeout_seconds=min(15, snapshot.timeout_seconds),
                    thinking_budget_tokens=0,
                ),
            )
        return result

    async def imports(self, params):
        self._app()
        return {
            "modules": sorted(sys.modules),
            "interpreter": sys.executable,
            "pythonBasePrefix": sys.base_prefix,
            "peakRssBytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "distributions": sorted(
                {
                    dist.metadata["Name"]: dist.version for dist in distributions()
                }.items()
            ),
        }

    def _environment_registry(self, params, *, mutation=False):
        self._app()
        if self.environments is None:
            raise PublicRpcError("environment_registry_unavailable")
        if mutation and params.get("target") != self._environment_target():
            raise PublicRpcError("environment_target_changed")
        return self.environments

    def _environment_target(self):
        return {"authorityId": self.identity.authority_id,
                "workspaceId": self.identity.workspace_id,
                "controllerEpoch": self.epoch}

    async def environment_list(self, params):
        registry = self._environment_registry(params)
        if set(params) - {"after", "limit"}:
            raise PublicRpcError("invalid_environment_request")
        missing = []
        for name in ("jupyter_client", "zmq"):
            try:
                importlib.import_module(name)
            except Exception:
                missing.append(name)
        try:
            page = registry.page(after=params.get("after", ""), limit=params.get("limit", 20))
        except ValueError as error:
            raise PublicRpcError(str(error)) from error
        return {**page, "schemaVersion": REGISTRY_SCHEMA_VERSION,
                "target": self._environment_target(),
                "controlClient": {"ready": not missing, "missing": missing},
                "notebook": {"enabled": True, "reason": "", "ownerRpcAvailable": True},
                "maintenance": {"enabled": True, "reason": ""}}

    async def environment_register(self, params):
        registry = self._environment_registry(params, mutation=True)
        if set(params) != {"target", "spec"}:
            raise PublicRpcError("invalid_environment_request")
        try:
            return {"environment": await registry.register(params["spec"])}
        except ValueError as error:
            raise PublicRpcError(str(error)) from error

    async def environment_preflight(self, params):
        registry = self._environment_registry(params, mutation=True)
        if set(params) != {"target", "environmentId"}:
            raise PublicRpcError("invalid_environment_request")
        try:
            return {"environment": await registry.preflight(params["environmentId"])}
        except ValueError as error:
            raise PublicRpcError(str(error)) from error

    async def environment_accept(self, params):
        registry = self._environment_registry(params, mutation=True)
        if set(params) != {"target", "environmentId", "revision"}:
            raise PublicRpcError("invalid_environment_request")
        try:
            return {"environment": await asyncio.to_thread(registry.accept, params["environmentId"], params["revision"])}
        except ValueError as error:
            raise PublicRpcError(str(error)) from error

    async def notebook_request(self, method, params):
        self._app()
        if self.notebook_client is None:
            raise PublicRpcError("notebook_owner_unavailable")
        target = params.get("target")
        base = self._environment_target()
        if not isinstance(target, dict) or any(target.get(k) != v for k, v in base.items()):
            raise PublicRpcError("notebook_target_changed")
        try:
            health = await self.notebook_client.attach()
            fence = {**base, "ownerId": health["owner"]["ownerId"],
                     "ownerEpoch": health["owner"]["ownerEpoch"]}
            if method == "attach":
                if set(params) != {"target"} or set(target) != set(base):
                    raise PublicRpcError("invalid_notebook_command")
                return {**health, "writeFence": fence}
            if target != fence:
                raise PublicRpcError("notebook_target_changed")
            result = await self.notebook_client.call(
                {"run": "admit", "selectEnvironment": "select_environment", "artifactInfo": "artifact_info", "artifactChunk": "artifact_chunk"}.get(method, method), {k:v for k,v in params.items() if k != "target"},
                expected_owner={k: target[k] for k in ("ownerId", "ownerEpoch")},
            )
            receipt = result.get("receipt") if method == "receipt" else result
            if method in {"run", "admit", "save"} or (method == "bind" and params.get("takeControl")):
                nid = params.get("notebookId") or params.get("command", {}).get("notebookId")
                snapshot = await self.notebook_client.call("snapshot", {"notebookId": nid, "limit": 1},
                    expected_owner={k: target[k] for k in ("ownerId", "ownerEpoch")})
                if snapshot["binding"]:
                    for language in ("python", "r"):
                        self.interactions.invalidate_tool(snapshot["binding"]["session_id"], language, "Notebook state changed under user control; prepare a new request after Resume.")
            if isinstance(receipt, dict) and receipt.get("kind") == "resume":
                receipt["newTurnRequired"] = not receipt.get("continuationPrepared", False) and not self.notebook_binding.waiters.get(receipt["sessionId"])
            return result
        except NotebookError as error:
            raise PublicRpcError(error.code) from error
        except (OSError, TimeoutError):
            raise PublicRpcError("notebook_owner_unavailable") from None

    async def aclose(self):
        if self.maintenance_jobs_task:
            self.maintenance_jobs_task.cancel()
            await asyncio.gather(self.maintenance_jobs_task, return_exceptions=True)
        # EOF stops new model and side-effect admission before draining shared state.
        for task in list(self.observers.values()):
            task.cancel()
        await asyncio.gather(*self.observers.values(), return_exceptions=True)
        if self.app is not None:
            await self.app.sessions.shutdown(0)
            if self.jobs is not None:
                await self.jobs.shutdown_observations()
            await self.app.aclose()
        for factory in self.factories:
            await factory.aclose()
        if self.jobs is not None:
            self.jobs.store.database.close()
        self.owner.close()


async def run_stdio(workspace: Path):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)

    async def write(raw):
        writer.write(raw)
        await writer.drain()

    peer = JsonPeer(reader, write)
    controller = Controller(workspace, peer)
    try:
        await peer.run()
    finally:
        await controller.aclose()
        writer.close()


def _parse_sandbox_feature(spec):
    if spec is None:
        return {}
    if isinstance(spec, bool):
        return {"sandbox": SandboxMode.AUTO if spec else SandboxMode.OFF}
    if isinstance(spec, str):
        try:
            return {"sandbox": _as_sandbox_mode(spec)}
        except AppConfigError as exc:
            raise RpcError("invalid controller sandbox configuration") from exc
    if not isinstance(spec, dict):
        raise RpcError("invalid controller sandbox configuration")
    allowed = {
        "mode",
        "backend",
        "egress",
        "egressGroups",
        "egress_groups",
        "egressAllow",
        "egress_allow",
        "egressEndpoints",
        "egress_endpoints",
        "allowDomains",
        "allow_domain",
        "allow_domains",
        "allowPrivateCidr",
        "allow_private_cidr",
        "kernels",
        "required",
        "autoApprove",
        "auto_approve",
    }
    if set(spec) - allowed:
        raise RpcError("invalid controller sandbox configuration")
    out = {}
    try:
        if "mode" in spec:
            if isinstance(spec["mode"], bool):
                out["sandbox"] = SandboxMode.AUTO if spec["mode"] else SandboxMode.OFF
            elif isinstance(spec["mode"], str):
                out["sandbox"] = _as_sandbox_mode(spec["mode"])
            else:
                raise RpcError("invalid controller sandbox configuration")
        if "backend" in spec:
            if not isinstance(spec["backend"], str):
                raise RpcError("invalid controller sandbox configuration")
            out["sandbox_backend"] = _as_sandbox_backend(spec["backend"])
        if "egress" in spec:
            if not isinstance(spec["egress"], str):
                raise RpcError("invalid controller sandbox configuration")
            out["sandbox_egress"] = _as_sandbox_egress(spec["egress"])
        for src_key, dst_key in (
            ("egressGroups", "sandbox_egress_groups"),
            ("egress_groups", "sandbox_egress_groups"),
            ("egressAllow", "sandbox_egress_allow"),
            ("egress_allow", "sandbox_egress_allow"),
            ("egressEndpoints", "sandbox_egress_endpoints"),
            ("egress_endpoints", "sandbox_egress_endpoints"),
            ("allowDomains", "sandbox_allow_domain"),
            ("allow_domain", "sandbox_allow_domain"),
            ("allow_domains", "sandbox_allow_domain"),
            ("allowPrivateCidr", "sandbox_allow_private_cidr"),
            ("allow_private_cidr", "sandbox_allow_private_cidr"),
        ):
            if src_key in spec:
                val = spec[src_key]
                if isinstance(val, str):
                    out[dst_key] = _as_csv_tuple(val)
                elif isinstance(val, (list, tuple)) and all(
                    isinstance(x, str) for x in val
                ):
                    out[dst_key] = tuple(x.strip() for x in val if x.strip())
                else:
                    raise RpcError("invalid controller sandbox configuration")
        for src_key, dst_key in (
            ("kernels", "sandbox_kernels"),
            ("required", "sandbox_required"),
            ("autoApprove", "sandbox_auto_approve"),
            ("auto_approve", "sandbox_auto_approve"),
        ):
            if src_key in spec:
                if type(spec[src_key]) is not bool:
                    raise RpcError("invalid controller sandbox configuration")
                out[dst_key] = spec[src_key]
    except AppConfigError as exc:
        raise RpcError("invalid controller sandbox configuration") from exc
    return out


def _resolve_sandbox_kwargs(params, *, production: bool, features: dict):
    try:
        resolved = {
            key: value
            for key, value in _from_env(os.environ).items()
            if key == "sandbox" or key.startswith("sandbox_")
        }
    except AppConfigError as exc:
        raise RpcError(str(exc)) from exc
    if "sandbox" in features:
        resolved.update(_parse_sandbox_feature(features["sandbox"]))
    if "sandbox" in params:
        resolved.update(_parse_sandbox_feature(params["sandbox"]))
    if production and "sandbox" not in resolved:
        resolved["sandbox"] = SandboxMode.AUTO
    return resolved

