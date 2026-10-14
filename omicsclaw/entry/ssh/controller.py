"""Session-scoped stdio facade. Business state remains in shared managers."""

from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import json
import os
import resource
import sys
import uuid
from importlib.metadata import distributions
from pathlib import Path

from omicsclaw.entry import AppConfig, attach_sessions, build_app
from omicsclaw.entry.assembly import DeploymentCapabilities
from omicsclaw.entry.config import SkillEnvMode
from omicsclaw.entry.desktop.interactions import DesktopInteractions, answer_permission
from omicsclaw.entry.desktop.jobs_manager import JobsManager
from omicsclaw.entry.desktop.turn_observation import DesktopChatSSEBody
from omicsclaw.entry.desktop.turn_submission import decode_chat_stream_request
from omicsclaw.entry.memory import memory_db_path
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEventLog
from omicsclaw.provider import ModelLimits, ProviderDialect

from .model_bridge import ModelSnapshot, bridged_provider
from .protocol import MAX_FRAME_BYTES, JsonPeer, RpcError

PROTOCOL_VERSION = 1
COMPONENT_VERSION = "0.1.0"


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
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(self.fd, 0o600)
            os.ftruncate(self.fd, 0)
            os.write(
                self.fd,
                json.dumps({"instanceId": instance_id, "pid": os.getpid()}).encode(),
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
        self.interactions = None
        self.jobs = None
        self.factory = None
        self.model_snapshot = None
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
                "chat.permission": self.permission,
                "chat.abort": self.abort,
                "request.lookup": self.lookup,
                "events.replay": self.replay,
                "sessions.list": self.sessions,
                "runtime.imports": self.imports,
            }
        )

    async def hello(self, params):
        if params.get("protocolVersion") != PROTOCOL_VERSION:
            raise RpcError("unsupported protocol")
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "componentVersion": COMPONENT_VERSION,
            "controllerInstanceId": self.instance_id,
            "epoch": self.epoch,
            "workspacePath": str(self.workspace),
            "workspaceId": self.identity.workspace_id,
            "authorityId": self.identity.authority_id,
            "durableIdentity": dataclasses.asdict(self.identity),
            "capabilities": ["base", "sdk_model_bridge", "durable_admission"],
        }

    async def configure(self, params):
        if self.app is not None:
            raise RpcError("configuration is immutable for this connection")
        permitted = {field.name for field in dataclasses.fields(ModelSnapshot)}
        if set(params) - permitted:
            raise RpcError("unknown model snapshot fields")
        snapshot = ModelSnapshot(
            **{
                **params,
                "dialect": ProviderDialect(params["dialect"]),
                "allowed_models": tuple(params["allowed_models"]),
            }
        )
        provider, self.factory = bridged_provider(snapshot, self.peer)
        config = AppConfig(
            workspace=self.workspace,
            provider=snapshot.provider_id,
            model=snapshot.model,
            durable_turn_events=True,
            skill_env=SkillEnvMode.OFF,
            skills_dir=Path(__file__).resolve().parents[3] / "skills",
        )
        self.app = attach_sessions(
            build_app(
                config,
                provider=provider,
                resolved_model=snapshot.model,
                model_limits=ModelLimits(
                    snapshot.context_tokens, snapshot.output_tokens
                ),
                capabilities=DeploymentCapabilities(notebook=False),
            ),
            abandon_grace_s=0,
        )
        if self.app.sessions.durable_identity is None:
            await self.app.aclose()
            self.app = None
            raise RpcError("durable admission capability required")
        if self.app.sessions.durable_identity != self.identity:
            await self.app.aclose()
            self.app = None
            raise RpcError("workspace authority identity changed")
        self.interactions = DesktopInteractions(self.app)
        self.jobs = JobsManager(self.app, register_default_runners=False)
        self.model_snapshot = snapshot
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
        if not request.source_request_id:
            raise RpcError("source request identity required")
        if request.resume:
            raise RpcError("resume must observe durable events; it cannot submit")
        if request.workspace and Path(request.workspace).resolve() != self.workspace:
            raise RpcError("workspace mismatch")
        inbound = request.to_inbound()
        # Permission selection is part of this admission, so a conflicting
        # redelivery cannot change approvals before its receipt is checked.
        inbound = dataclasses.replace(
            inbound,
            values={
                **inbound.values,
                "__desktop_permission_profile__": request.permission_profile,
            },
        )
        handle, newly_admitted = await app.sessions.admit(inbound)
        if newly_admitted and request.permission_profile:
            self.interactions.set_permission_profile(
                request.session_id, request.permission_profile
            )
        self.interactions.remember_request(
            handle.session_id, request.source_request_id, handle.turn_id
        )
        if newly_admitted:
            task = asyncio.create_task(self._observe(handle))
            self.observers[handle.turn_id] = task
            task.add_done_callback(
                lambda done: self._observer_done(handle.turn_id, done)
            )
        return {"turnId": handle.turn_id, "sessionId": handle.session_id}

    def _observer_done(self, turn_id, task):
        if self.observers.get(turn_id) is task:
            self.observers.pop(turn_id, None)
        if not task.cancelled():
            task.exception()

    async def _observe(self, handle):
        body = DesktopChatSSEBody(
            handle.observe(),
            keepalive_s=None,
            approvals=handle.approvals,
            interactions=self.interactions,
            provider=self.model_snapshot.provider_id,
            model=self.model_snapshot.model,
        )
        try:
            async with body:
                async for frame in body:
                    # The shared projector owns approval registration and wire semantics.
                    await handle.stream.flush_durable()
                    await self.peer.send(
                        {
                            "kind": "event",
                            "method": "chat.frame",
                            "params": {
                                "turnId": handle.turn_id,
                                "sessionId": handle.session_id,
                                "cursor": body.last_seq,
                                "frame": frame,
                                "durableIdentity": dataclasses.asdict(
                                    self.app.sessions.durable_identity
                                ),
                            },
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            handle.cancel()
            raise

    async def permission(self, params):
        return await answer_permission(self._app(), self.interactions, params)

    async def abort(self, params):
        handle = self._app().sessions.handle(params["turnId"])
        handle.cancel()
        return {"accepted": True}

    async def lookup(self, params):
        receipt = self._app().sessions.request_receipt(
            params["sessionId"], params["sourceRequestId"]
        )
        return {"receipt": receipt}

    async def replay(self, params):
        app = self._app()
        if not app.memory:
            raise RpcError("durable state required")
        session_id, turn_id = params["sessionId"], params["turnId"]
        log = DurableTurnEventLog(app.memory.database)
        stream_id = json.dumps([session_id, turn_id], separators=(",", ":"))
        events = log.replay(stream_id, after_seq=int(params.get("afterSeq", 0)))
        return {"events": [dataclasses.asdict(event) for event in events]}

    async def sessions(self, params):
        return {
            "sessions": [
                {"sessionId": row.session_id, "updatedAt": row.updated_at}
                for row in await self._app().sessions.list_sessions(limit=100)
            ]
        }

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

    async def aclose(self):
        # EOF stops new model and side-effect admission before draining shared state.
        for task in list(self.observers.values()):
            task.cancel()
        await asyncio.gather(*self.observers.values(), return_exceptions=True)
        if self.app is not None:
            await self.app.sessions.shutdown(0)
            await self.app.aclose()
        if self.factory is not None:
            await self.factory.aclose()
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
