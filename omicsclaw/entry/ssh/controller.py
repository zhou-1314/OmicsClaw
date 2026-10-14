"""Session-scoped stdio facade. Business state remains in shared managers."""

from __future__ import annotations

import asyncio
import dataclasses
import contextvars
from contextlib import contextmanager
import fcntl
import json
import os
import resource
import socket
import pwd
import re
import stat
import sys
import uuid
from importlib.metadata import distributions
from pathlib import Path

from omicsclaw.entry import AppConfig, attach_sessions, build_app
from omicsclaw.entry.assembly import DeploymentCapabilities
from omicsclaw.entry.config import SkillEnvMode
from omicsclaw.entry.desktop.chat_service import admit_chat
from omicsclaw.entry.desktop.interactions import DesktopInteractions, answer_permission
from omicsclaw.entry.desktop.jobs_manager import JobsManager
from omicsclaw.entry.desktop.turn_observation import DesktopChatSSEBody
from omicsclaw.entry.desktop.turn_submission import (
    decode_chat_stream_request,
    ingress_identity,
)
from omicsclaw.entry.memory import memory_db_path
from omicsclaw.memory.database import Database
from omicsclaw.memory.turn_events import DurableTurnEventLog
from omicsclaw.observability import Telemetry
from omicsclaw.provider import ModelLimits, ProviderDialect, ProviderConfig
from omicsclaw.tools.context import context_value
from omicsclaw.entry.shadow_review import review_with_provider
from omicsclaw.entry.desktop.title import generate_title
from omicsclaw.entry.desktop.provider_probe import probe_provider
from omicsclaw.entry.desktop.event_service import replay_frames
from omicsclaw.entry.desktop.service import DesktopService

from .model_bridge import ModelSnapshot, bridged_provider
from .manifest import verified_source_metadata
from .protocol import MAX_FRAME_BYTES, JsonPeer, RpcError

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
                "desktop.request": self.desktop_request,
                "model.title": self.title,
                "model.test": self.provider_test,
            }
        )

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
            ],
        }

    async def configure(self, params):
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
        if production:
            if (
                set(params) - {"schemaVersion", "roles", "installationId", "features"}
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
                or set(features) != {"shadowReview"}
                or type(features["shadowReview"]) is not bool
            ):
                raise RpcError("invalid controller feature snapshot")
            snapshots = params["roles"]
        else:
            snapshots = {"main": params}
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
                telemetry=Telemetry(),
                provider_for_role=(
                    self.role_providers.__getitem__ if production else None
                ),
                review_call=review_call if production else None,
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
                deployment_values=(
                    {
                        "__desktop_model_snapshot__": {
                            "provider": self.model_snapshot.provider_id,
                            "model": self.model_snapshot.model,
                            "revision": self.model_snapshot.revision,
                        }
                    }
                    if self.production
                    else None
                ),
            )
        handle, newly_admitted = settled.handle, settled.newly_admitted
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
        receipt = DurableTurnEventLog(self.app.memory.database).turn_receipt(
            handle.session_id, handle.turn_id
        )
        source_request_id = (
            self.caller_request_id(receipt["source_request_id"]) if receipt else ""
        )
        body = DesktopChatSSEBody(
            handle.observe(),
            keepalive_s=None,
            approvals=handle.approvals,
            interactions=self.interactions,
            provider=self.model_snapshot.provider_id,
            model=self.model_snapshot.model,
            compaction=handle if handle.compaction_only else None,
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
            params["sessionId"], self.request_key(params["sourceRequestId"])
        )
        return {"receipt": receipt}

    async def requests(self, params):
        app = self._app()
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

    async def aclose(self):
        # EOF stops new model and side-effect admission before draining shared state.
        for task in list(self.observers.values()):
            task.cancel()
        await asyncio.gather(*self.observers.values(), return_exceptions=True)
        if self.app is not None:
            await self.app.sessions.shutdown(0)
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
