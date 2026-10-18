"""Independent Notebook ownership around the shared execution state machine."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import signal
import sqlite3
import time
import uuid
from pathlib import Path

from omicsclaw.kernel.manager import PersistentKernelManager
from omicsclaw.kernel.r_launch import RegisteredRLaunch, identity_code
from omicsclaw.kernel.session import OutputLimits, SessionKernel
from omicsclaw.memory.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    hash_and_size,
    new_artifact_id,
)
from omicsclaw.memory.database import Database
from omicsclaw.sandbox.manager import SandboxManager
from omicsclaw.tools.builtin.bash import without_control_credentials

from .actions import OwnerActions
from .analysis import OwnerAnalysis
from .analysis_store import SCHEMA as ANALYSIS_SCHEMA
from .artifacts import OwnerArtifacts
from .contracts import (
    MAX_CONTROL_RSS_BYTES,
    MAX_DISK_BYTES,
    MAX_KERNELS,
    MAX_OUTPUT_BYTES,
    MAX_PENDING,
    MAX_SCIENCE_RSS_BYTES,
    MAX_TIMEOUT_SECONDS,
    OWNER_PROTOCOL,
    bounded,
    command,
    encoded,
)
from .helpers import SCHEMA as HELPER_SCHEMA
from .helpers import OwnerHelpers
from .output import OwnerOutputStream
from .policy import policy_digest
from .request_state import SCHEMA as REQUEST_SCHEMA
from .request_state import RequestState
from .security import (
    environment_gate,
    private_directory,
    private_file,
    process_evidence,
    rss_bytes,
    same_process,
    tree_rss,
)
from .service import TERMINAL, NotebookAnalysis, NotebookError, identifier, revision
from .store import (
    SCHEMA,
    OwnerViews,
    bump,
    current_kernel,
    kernel_row,
    kernel_set,
    migrate,
    next_generation,
    require_kernel,
)


class OwnerRuntime:
    """The process lock is held by the caller for this object's entire lifetime."""

    def __init__(
        self,
        workspace,
        identity,
        *,
        source_digest,
        env,
        config=None,
        limits=None,
        registry_probe=None,
        manager=None,
        environment_coordinator=None,
    ):
        self.workspace = Path(workspace).resolve(strict=True)
        private_directory(self.workspace / ".omicsclaw")
        self.owner_id = uuid.uuid4().hex
        self.source_digest = source_digest
        self.identity = identity
        self.env = without_control_credentials(env)
        self.config = config
        self.policy_digest = policy_digest(config)
        self.limits = self.validate_limits(limits or {})
        for filename in ("notebooks.db", "memory.db", "notebook-artifacts.db"):
            os.close(private_file(self.workspace / ".omicsclaw" / filename))
        self.db = Database(self.workspace / ".omicsclaw/notebooks.db")
        self.registry_db = Database(self.workspace / ".omicsclaw/memory.db")
        self.artifact_db = Database(self.workspace / ".omicsclaw/notebook-artifacts.db")
        self.artifacts = ArtifactStore(self.artifact_db)
        self.sandbox = None
        self.lifecycle_wrapper = None
        self.controller_process = None
        self.registry_probe = registry_probe
        self.helpers = OwnerHelpers(self)
        self.manager = manager or PersistentKernelManager(
            self.workspace,
            manual_lifecycle=True,
            kernel_factory=self._start_kernel,
            on_figure=self._figure,
            on_kernel_ready=self._inject_helpers,
            limits=OutputLimits(
                stream_emit_max=2 * 1024 * 1024,
                cell_buffer_max=8192,
                result_max=8192,
                figure_count_max=16,
                figure_encoded_max=2 * 1024 * 1024,
            ),
        )
        self.service = NotebookAnalysis(self.db, self.manager, coordination=self)
        self.service.owner_id = self.owner_id
        self.store_identity = {
            "authorityId": self.service.events.identity.authority_id,
            "workspaceId": self.service.events.identity.workspace_id,
        }
        self.operation_lock = asyncio.Lock()
        self.authorization = contextvars.ContextVar(
            "notebook_mutation_authorization", default=None
        )
        self.admission_environment = contextvars.ContextVar(
            "notebook_admission_environment", default=None
        )
        self.checkpoint_operation = contextvars.ContextVar(
            "notebook_checkpoint_operation", default=None
        )
        self.analysis_operation = contextvars.ContextVar(
            "notebook_analysis_operation", default=None
        )
        self.running_notebooks = {}
        self.wakeup = asyncio.Event()
        self.streams = {}
        self.background = set()
        self.durability_failed = False
        self.closed = False
        self.scheduler = self.heartbeat = None
        self.db.run(lambda c: c.executescript(SCHEMA))
        self.db.run(migrate)
        self.db.run(lambda c: c.executescript(REQUEST_SCHEMA))
        self.db.run(lambda c: c.executescript(HELPER_SCHEMA))
        self.db.run(lambda c: c.executescript(ANALYSIS_SCHEMA))
        self.analysis = OwnerAnalysis(self)
        from .checkpoints import OwnerCheckpoints

        self.checkpoints = OwnerCheckpoints(self)
        from .restores import OwnerRestores

        self.restores = OwnerRestores(self)
        self.requests = RequestState(self)
        self.epoch = self.service._transaction(self._take_ownership)
        self.views = OwnerViews(self)
        self.actions = OwnerActions(self)
        self.artifact_reader = OwnerArtifacts(self)
        self.maintenance_service = None
        self.environment_guard = None
        if environment_coordinator is not None:
            from .environment_guard import OwnerEnvironmentGuard

            self.environment_guard = OwnerEnvironmentGuard(
                self, environment_coordinator
            )

    @staticmethod
    def validate_limits(values):
        # Runtime refusal limits are distinct from profiles/budgets.json's
        # unchanged release RSS gate for controller plus owner.
        defaults = {
            "kernels": MAX_KERNELS,
            "pending": MAX_PENDING,
            "outputBytes": MAX_OUTPUT_BYTES,
            "diskBytes": MAX_DISK_BYTES,
            "controlRssBytes": MAX_CONTROL_RSS_BYTES,
            "scienceRssBytes": MAX_SCIENCE_RSS_BYTES,
            "timeoutSeconds": MAX_TIMEOUT_SECONDS,
            "checkpointObjects": 32,
            "checkpointFileBytes": 256 * 1024 * 1024,
            "checkpointBytes": 512 * 1024 * 1024,
            "checkpointStagingBytes": 128 * 1024 * 1024,
            "checkpointTimeoutSeconds": 120,
        }
        if (
            not isinstance(values, dict)
            or set(values) - set(defaults)
            or any(
                type(v) is not int or not 1 <= v <= defaults[k]
                for k, v in values.items()
            )
        ):
            raise NotebookError("invalid_notebook_limits")
        return {**defaults, **values}

    def _take_ownership(self, c):
        self.checkpoints.store.recover(c)
        self.restores.recover(c)
        row = c.execute(
            "SELECT * FROM notebook_runtime_store WHERE singleton=1"
        ).fetchone()
        if row and (
            row["schema_version"] != OWNER_PROTOCOL
            or row["authority_id"] != self.identity["authorityId"]
            or row["workspace_id"] != self.identity["workspaceId"]
            or row["workspace_path"] != str(self.workspace)
        ):
            raise NotebookError("notebook_store_mismatch", 409)
        epoch = row["owner_epoch"] + 1 if row else 1
        c.execute(
            "INSERT INTO notebook_runtime_store VALUES(1,?,?,?,?,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET owner_id=excluded.owner_id,"
            "owner_epoch=excluded.owner_epoch,source_digest=excluded.source_digest",
            (
                OWNER_PROTOCOL,
                self.identity["authorityId"],
                self.identity["workspaceId"],
                str(self.workspace),
                self.owner_id,
                epoch,
                self.source_digest,
            ),
        )
        c.execute(
            "UPDATE notebook_owners SET owner_id=?,lease_until=?",
            (self.owner_id, time.time() + 30),
        )
        # A committed outcome survives an owner crash; an unfinished event
        # flush does not become complete by inference. Preserve the result and
        # expose the missing durability confirmation without replaying code.
        pending = c.execute(
            "SELECT e.execution_id,e.notebook_id FROM notebook_executions e JOIN "
            "notebook_runtime_executions x USING(execution_id) WHERE "
            "e.status IN ('completed','failed','interrupted') AND "
            "json_extract(e.output_json,'$.event_stream_pending')=1"
        )
        for item in pending:
            c.execute(
                "UPDATE notebook_executions SET output_json=json_set(output_json,"
                "'$.event_stream_pending',json('false'),'$.durable_error',"
                "'event_log_incomplete_after_owner_loss') WHERE execution_id=?",
                (item["execution_id"],),
            )
            c.execute(
                "UPDATE notebook_runtime_executions SET durability='failed' WHERE execution_id=?",
                (item["execution_id"],),
            )
            bump(c, item["notebook_id"])
        # An owner crash is different from a disconnected controller. We retain
        # uncertain executions and never infer completion from an absent writer.
        rows = c.execute(
            "SELECT * FROM notebook_runtime_kernels WHERE state NOT IN ('released','lost') LIMIT ?",
            (MAX_KERNELS + 1,),
        ).fetchall()
        for kernel in rows:
            evidence = json.loads(kernel["process_json"] or "null")
            state = "unconfirmed" if same_process(evidence) else "lost"
            if kernel["state"] == "reserved" and evidence is None:
                state = "reserved"
            c.execute(
                "UPDATE notebook_runtime_kernels SET state=?,confirmed_at=? WHERE instance_id=?",
                (state, time.time(), kernel["instance_id"]),
            )
            rows2 = c.execute(
                "SELECT x.execution_id,x.dispatch_state FROM notebook_runtime_executions x "
                "JOIN notebook_executions e USING(execution_id) WHERE x.instance_id=? "
                "AND e.status IN ('queued','running') LIMIT ?",
                (kernel["instance_id"], MAX_PENDING + 1),
            ).fetchall()
            for execution in rows2:
                if execution["dispatch_state"] == "pending" and state == "reserved":
                    c.execute(
                        "UPDATE notebook_runtime_executions SET owner_epoch=? WHERE execution_id=?",
                        (epoch, execution["execution_id"]),
                    )
                    c.execute(
                        "INSERT INTO notebook_owners VALUES(?,?,?) ON CONFLICT(notebook_id) "
                        "DO UPDATE SET owner_id=excluded.owner_id,lease_until=excluded.lease_until",
                        (kernel["notebook_id"], self.owner_id, time.time() + 30),
                    )
                    c.execute(
                        "UPDATE notebook_executions SET status='queued' WHERE execution_id=?",
                        (execution["execution_id"],),
                    )
                    c.execute(
                        "UPDATE notebook_cells SET status='queued',revision=revision+1 WHERE execution_id=?",
                        (execution["execution_id"],),
                    )
                    continue
                eid = execution["execution_id"]
                c.execute(
                    "UPDATE notebook_executions SET status='outcome_unknown' WHERE execution_id=?",
                    (eid,),
                )
                c.execute(
                    "UPDATE notebook_cells SET status='outcome_unknown',revision=revision+1 "
                    "WHERE execution_id=?",
                    (eid,),
                )
                c.execute(
                    "UPDATE notebook_runtime_executions SET dispatch_state='unknown',durability='unknown' WHERE execution_id=?",
                    (eid,),
                )
                self.requests.changed(c, kernel["notebook_id"], eid)
            bump(c, kernel["notebook_id"])
        return epoch

    def fence(self, c):
        row = c.execute(
            "SELECT owner_id,owner_epoch FROM notebook_runtime_store WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] != self.owner_id or row[1] != self.epoch:
            raise NotebookError("notebook_owner_changed", 409)

    async def start(self):
        from omicsclaw.entry.ssh.environments import EnvironmentRegistry

        if self.config is not None and self.config.mode != "off":
            import dataclasses
            from omicsclaw.entry.sandbox import (
                EGRESS_GRANTS_FILENAME,
                load_persisted_egress_grants,
            )

            persisted = load_persisted_egress_grants(
                self.workspace / ".omicsclaw" / EGRESS_GRANTS_FILENAME
            )
            owner_root = Path(__file__).resolve().parents[3]
            owner_code_mounts = tuple(
                path
                for path in (owner_root / "omicsclaw", owner_root / "skills")
                if path.is_dir()
                and self.workspace != owner_root
                and self.workspace not in owner_root.parents
            )
            sandbox_cfg = dataclasses.replace(
                self.config,
                egress_allow=tuple(
                    dict.fromkeys((*self.config.egress_allow, *persisted))
                ),
                extra_allowed_domains=self.config.extra_allowed_domains
                | frozenset(persisted),
                extra_read_paths=tuple(
                    dict.fromkeys((*self.config.extra_read_paths, *owner_code_mounts))
                ),
                read_only_mounts=tuple(
                    dict.fromkeys((*self.config.read_only_mounts, *owner_code_mounts))
                ),
            )
            self.sandbox = SandboxManager(
                self.workspace, config=sandbox_cfg, env_provider=lambda: self.env
            )
            await self.sandbox.start()
            self._synced_sandbox_domains = set(persisted)
            self._sync_sandbox_grants()
        wrapper = self.sandbox.as_kernel_spawn_wrapper() if self.sandbox else None
        if wrapper is None:
            import sys

            from omicsclaw.entry.process_containment import SubreaperLifecycleWrapper

            if sys.platform.startswith("linux"):
                self.lifecycle_wrapper = SubreaperLifecycleWrapper(
                    private_directory(self.workspace / ".omicsclaw/process-lifecycles")
                )
        options = {"wrapper": wrapper}
        if self.environment_guard is not None:
            from omicsclaw.entry.environment_reads import EnvironmentReadGuard

            options["read_guard"] = EnvironmentReadGuard(
                self.environment_guard.coordinator,
                self.workspace,
                self.identity["workspaceId"],
                self.environment_guard.owner,
            )
        if self.registry_probe is not None:
            options["probe"] = self.registry_probe
        self.registry = EnvironmentRegistry(self.registry_db, self.workspace, **options)
        if self.environment_guard is not None:
            await asyncio.to_thread(self.environment_guard.recover)
            from omicsclaw.entry.maintenance import MaintenanceService

            self.maintenance_service = MaintenanceService(self)
            await self.maintenance_service.start()
        self.scheduler = asyncio.create_task(self._schedule())
        self.heartbeat = asyncio.create_task(self._heartbeat())
        self.wakeup.set()

    def health(self):
        return bounded(
            {
                "owner": {
                    "ownerId": self.owner_id,
                    "ownerEpoch": self.epoch,
                    "process": process_evidence(os.getpid()),
                    "protocol": OWNER_PROTOCOL,
                    "sourceDigest": self.source_digest,
                    "state": "ready",
                    "lifecycle": "detached_process",
                    "sessionScopeVerified": False,
                },
                "store": self.store_identity,
                "capabilities": [
                    "notebook_model_v1",
                    "ordered_outputs_v1",
                    "fixed_helpers_v1",
                    "notebook_r_v1",
                    "notebook_analysis_state_v1",
                    "notebook_analysis_run_context_v1",
                    "notebook_checkpoints_v1",
                    "notebook_checkpoint_restore_v1",
                    *(
                        ["environment_maintenance_v1"]
                        if self.maintenance_service
                        else []
                    ),
                ],
                "languages": ["python", "r"],
                "limits": self.limits,
                "durability": "failed" if self.durability_failed else "ready",
                "sandbox": {
                    "backend": str(self.sandbox.backend_name)
                    if self.sandbox
                    else "off",
                    "level": str(self.sandbox.level) if self.sandbox else "off",
                    "policyDigest": self.policy_digest,
                    "policyVersion": 1,
                    "egress": str(self.config.egress) if self.config else "open",
                    "kernelIsolation": bool(
                        self.sandbox and self.config.sandbox_kernels
                    ),
                    "reason": ""
                    if self.sandbox and self.config.sandbox_kernels
                    else "same_uid_processes_are_not_isolated",
                },
                "processLifecycle": {
                    "hostSubreaperWrapper": self.lifecycle_wrapper is not None,
                    "exitProof": "actual_namespace_init_or_subreaper_echild_receipt",
                    "scope": "descendants_only",
                    "reason": "actual_launch_evidence_required",
                },
                "lifecycleReason": "host_session_cleanup_requires_deployment_verification",
            }
        )

    def _own(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)

        def done(finished):
            self.background.discard(finished)
            if not finished.cancelled() and isinstance(
                finished.exception(), sqlite3.Error
            ):
                self.durability_failed = True

        task.add_done_callback(done)
        return task

    def _sync_sandbox_grants(self):
        if self.sandbox is None:
            return
        from omicsclaw.entry.sandbox import (
            EGRESS_GRANTS_FILENAME,
            load_effective_egress_grants,
        )

        granted, revoked = load_effective_egress_grants(
            self.workspace / ".omicsclaw" / EGRESS_GRANTS_FILENAME
        )
        current = set(granted)
        previous = getattr(self, "_synced_sandbox_domains", set())
        for domain in current - previous:
            self.sandbox.allow_domain_for_session(domain)
        for domain in (previous - current) | set(revoked):
            self.sandbox.revoke_domain(domain)
        self._synced_sandbox_domains = current

    async def mutate(self, method, params, authorize=None):
        # Once a mutation reaches this boundary its lifetime belongs to owner.
        self._sync_sandbox_grants()
        token = self.authorization.set(authorize)
        try:
            return await asyncio.shield(self._own(getattr(self, method)(params)))
        finally:
            self.authorization.reset(token)

    async def maintenance(self, params):
        if set(params) != {"action", "params"} or not isinstance(
            params["params"], dict
        ):
            raise NotebookError("invalid_environment_request")
        self.authorize()
        if self.maintenance_service is None:
            raise NotebookError("environment_maintenance_unavailable", 503)
        return await self.maintenance_service.call(params["action"], params["params"])

    def _inject_helpers(self, state):
        if self.db.run(
            lambda c: self.checkpoints.execution(c, state.current_execution_token)
        ):
            return
        self.analysis.selection_helpers.inject(state)
        self.helpers.inject(state)
        self.checkpoints.inject(state)

    async def analysis_command(self, params):
        return await self.analysis.command(params)

    async def analysis_agent(self, params):
        return await self.analysis.agent(params)

    async def check_analysis_request(self, params):
        if (
            not {"sessionId", "requestId", "token"}
            <= set(params)
            <= {"sessionId", "requestId", "token", "action"}
        ):
            raise NotebookError("invalid_notebook_command")
        async with self.operation_lock:
            self.authorize()

            def check(c):
                nid = self.analysis.check_agent(c, params)
                action = params.get("action", "")
                if action not in {
                    "state",
                    "page",
                    "checkpoint.retry",
                    "checkpoint.pin",
                }:
                    for language in ("python", "r"):
                        self.checkpoints.store.require_clear(
                            c,
                            nid,
                            language,
                            inspect=action in {"object.inspect", "selection.clear"},
                        )

            self.service._transaction(check)
            return {"current": True}

    def authorize(self):
        check = self.authorization.get()
        if check is not None:
            check()

    def _budget(self, *, new_kernel=False):
        if self.durability_failed:
            raise NotebookError("notebook_durability_failed", 503)
        counts = self.db.run(
            lambda c: (
                c.execute(
                    "SELECT count(*) FROM notebook_runtime_kernels WHERE state NOT IN ('released','lost')"
                ).fetchone()[0],
                c.execute(
                    "SELECT count(*) FROM notebook_executions WHERE status IN ('queued','running')"
                ).fetchone()[0],
            )
        )
        if (new_kernel and counts[0] >= self.limits["kernels"]) or counts[
            1
        ] >= self.limits["pending"]:
            raise NotebookError("notebook_resource_limit", 409)
        control_rss = rss_bytes(os.getpid())
        if (
            same_process(self.controller_process)
            and self.controller_process["pid"] != os.getpid()
        ):
            control_rss += rss_bytes(self.controller_process["pid"])
        if control_rss > self.limits["controlRssBytes"]:
            raise NotebookError("notebook_resource_limit", 409)
        science = 0
        for row in self.db.run(
            lambda c: c.execute(
                "SELECT process_json FROM notebook_runtime_kernels WHERE state NOT IN ('released','lost') LIMIT ?",
                (MAX_KERNELS + 1,),
            ).fetchall()
        ):
            evidence = json.loads(row[0] or "null")
            if same_process(evidence):
                science += tree_rss(evidence["pid"])
        if science > self.limits["scienceRssBytes"]:
            raise NotebookError("notebook_resource_limit", 409)
        usage = self.db.run(self.checkpoints.usage)
        size = usage["workspaceBytes"] + usage["reservedBytes"]
        if size > self.limits["diskBytes"]:
            raise NotebookError("notebook_resource_limit", 409)

    async def _ready(
        self, environment_id, environment_revision, *, read_reservation=None
    ):
        # Caller holds the workspace gate across this observation and its write.
        if read_reservation is not None and self.registry.read_guard:
            with self.registry.read_guard.draining(read_reservation):
                await self.registry._preflight(environment_id)
        else:
            await self.registry._preflight(environment_id)
        row = self.registry.require_ready(environment_id, environment_revision)
        if row["spec"]["language"] not in {"python", "r"}:
            raise NotebookError("notebook_language_unavailable", 409)
        return row

    async def context(self, params):
        if set(params) != {"sessionId", "requestId"}:
            raise NotebookError("invalid_notebook_command")
        async with self.operation_lock, environment_gate(self.workspace):
            self.authorize()
            return self.service._transaction(
                lambda c: self.requests.snapshot(
                    c,
                    params["sessionId"],
                    params["requestId"],
                )
            )

    async def check_request(self, params):
        if set(params) not in (
            {"sessionId", "requestId", "token"},
            {"sessionId", "requestId", "token", "language"},
        ) or params.get("language", "python") not in {"python", "r"}:
            raise NotebookError("invalid_notebook_command")
        async with self.operation_lock, environment_gate(self.workspace):
            self.authorize()
            self.service._transaction(
                lambda c: self.requests.verify(
                    c,
                    params["token"],
                    params["sessionId"],
                    params["requestId"],
                    language=params.get("language", "python"),
                )
            )
            return {"current": True}

    async def save(self, params):
        return await self.actions.save(params)

    async def register_helper(self, params):
        return await self.helpers.register(params)

    async def resume(self, params):
        return await self.actions.resume(params)

    async def select_environment(self, params):
        if set(params) not in (
            {"environmentId", "environmentRevision", "expectedRevision", "commandId"},
            {
                "environmentId",
                "environmentRevision",
                "expectedRevision",
                "commandId",
                "language",
            },
        ) or params.get("language", "python") not in {"python", "r"}:
            raise NotebookError("invalid_notebook_command")
        identifier(params["commandId"])
        revision(params["expectedRevision"])
        async with self.operation_lock, environment_gate(self.workspace):
            self.authorize()
            _, old = self.db.run(
                lambda c: self._receipt(c, params["commandId"], params)
            )
            if old:
                return old
            selected = await self._ready(
                params["environmentId"], params["environmentRevision"]
            )
            language = params.get("language", "python")
            if selected["spec"]["language"] != language:
                raise NotebookError("notebook_language_mismatch", 409)

            def work(c):
                self.authorize()
                self.fence(c)
                digest, old = self._receipt(c, params["commandId"], params)
                if old:
                    return old
                row = c.execute(
                    "SELECT revision FROM notebook_language_defaults WHERE language=?",
                    (language,),
                ).fetchone()
                if row[0] != params["expectedRevision"]:
                    raise NotebookError("revision_conflict", 409)
                c.execute(
                    "UPDATE notebook_language_defaults SET environment_id=?,environment_revision=?,revision=revision+1 WHERE language=?",
                    (params["environmentId"], params["environmentRevision"], language),
                )
                result = {
                    "environmentId": params["environmentId"],
                    "environmentRevision": params["environmentRevision"],
                    "revision": row[0] + 1,
                    "language": language,
                }
                c.execute(
                    "INSERT INTO notebook_runtime_commands VALUES(?,?,?)",
                    (params["commandId"], digest, encoded(result).decode()),
                )
                return result

            return self.service._transaction(work)

    def _receipt(self, c, command_id, document):
        digest = hashlib.sha256(encoded(document)).hexdigest()
        row = c.execute(
            "SELECT * FROM notebook_runtime_commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if row:
            if row["digest"] != digest:
                raise NotebookError("command_conflict", 409)
            return digest, json.loads(row["receipt_json"])
        return digest, None

    async def bind(self, params):
        fields = {
            "notebookId",
            "sessionId",
            "commandId",
            "environmentId",
            "environmentRevision",
        }
        if (
            not fields <= set(params) <= fields | {"language", "takeControl"}
            or params.get("language", "python") not in {"python", "r"}
            or type(params.get("takeControl", False)) is not bool
        ):
            raise NotebookError("invalid_notebook_command")
        for name in fields - {"environmentRevision"}:
            identifier(params[name])
        revision(params["environmentRevision"])
        async with self.operation_lock:
            self.authorize()
            _, old = self.db.run(
                lambda c: self._receipt(c, params["commandId"], params)
            )
            if old:
                return old
            self.db.run(
                lambda c: self.restores.require_clear(
                    c, params["notebookId"], params.get("language", "python")
                )
            )
            async with environment_gate(self.workspace):
                env = await self._ready(
                    params["environmentId"], params["environmentRevision"]
                )
                language = params.get("language", "python")
                if env["spec"]["language"] != language:
                    raise NotebookError("notebook_language_mismatch", 409)
                self._budget(new_kernel=True)
                instance = uuid.uuid4().hex
                generation = self.db.run(
                    lambda c: next_generation(c, params["notebookId"])
                )
                if self.environment_guard is not None:
                    self.environment_guard.kernel(
                        instance, params["notebookId"], generation, env
                    )

                def work(c):
                    self.authorize()
                    self.fence(c)
                    digest, old = self._receipt(c, params["commandId"], params)
                    if old:
                        return old
                    nid, sid = params["notebookId"], params["sessionId"]
                    self.restores.require_clear(c, nid, language)
                    kernel = current_kernel(c, nid, language)
                    if kernel and kernel["state"] not in {"released", "lost"}:
                        raise NotebookError("notebook_kernel_exists", 409)
                    binding = c.execute(
                        "SELECT * FROM notebook_bindings WHERE notebook_id=? OR session_id=? LIMIT 1",
                        (nid, sid),
                    ).fetchone()
                    if binding and (
                        binding["notebook_id"] != nid or binding["session_id"] != sid
                    ):
                        raise NotebookError("binding_conflict", 409)
                    key = "nk" + instance
                    now = time.time()
                    c.execute(
                        "INSERT INTO notebook_runtime_kernels(instance_id,notebook_id,generation,session_key,environment_id,environment_revision,environment_json,maintenance_fence,state,process_json,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,0,'reserved',NULL,?,?)",
                        (
                            instance,
                            nid,
                            generation,
                            key,
                            params["environmentId"],
                            params["environmentRevision"],
                            encoded(
                                {
                                    **env,
                                    "componentSourceDigest": self.source_digest,
                                    "kernelConnectionRef": f".omicsclaw/kernel/{key}/runtime/kernel.json",
                                }
                            ).decode(),
                            now,
                            now,
                        ),
                    )
                    c.execute(
                        "UPDATE notebook_runtime_kernels SET language=? WHERE instance_id=?",
                        (language, instance),
                    )
                    c.execute(
                        "INSERT INTO notebook_bindings VALUES(?,?,?,?) ON CONFLICT(notebook_id) "
                        "DO UPDATE SET kernel_session_id=excluded.kernel_session_id",
                        (
                            nid,
                            sid,
                            key
                            if language == "python"
                            else (binding["kernel_session_id"] if binding else ""),
                            now,
                        ),
                    )
                    self.requests.provenance(c, "kernel", instance)
                    if params.get("takeControl"):
                        self.requests.takeover(c, nid)
                    bump(c, nid)
                    result = {
                        "binding": {"notebook_id": nid, "session_id": sid},
                        "kernel": kernel_row(current_kernel(c, nid, language)),
                        "kernels": kernel_set(c, nid),
                        "notebookSeq": OwnerViews.sequence(c, nid),
                    }
                    c.execute(
                        "INSERT INTO notebook_runtime_commands VALUES(?,?,?)",
                        (params["commandId"], digest, encoded(result).decode()),
                    )
                    return result

                try:
                    result = bounded(self.service._transaction(work))
                    if self.environment_guard is not None:
                        self.environment_guard.commit_kernel(instance)
                    return result
                except BaseException:
                    if self.environment_guard is not None:
                        self.environment_guard.rollback_uncommitted("kernel", instance)
                    raise

    async def admit(self, params):
        return await self._admit(params, agent=False)

    async def admit_agent(self, params):
        return await self._admit(params, agent=True)

    async def _admit(
        self, params, *, agent, analysis_operation=None, checkpoint_operation=None
    ):
        self._sync_sandbox_grants()
        if set(params) != {"command"}:
            raise NotebookError("invalid_notebook_command")
        document, digest = command(params["command"])
        if (document["origin"] == "agent") != agent:
            raise NotebookError("invalid_notebook_command")
        eid = hashlib.sha256(
            json.dumps([document["notebookId"], document["commandId"]]).encode()
        ).hexdigest()
        async with self.operation_lock:
            self.authorize()
            # Access was authenticated by IPC before looking up an old receipt.
            old = self.db.run(
                lambda c: c.execute(
                    "SELECT digest FROM notebook_runtime_executions WHERE execution_id=?",
                    (eid,),
                ).fetchone()
            )
            if old:
                if old[0] != digest:
                    raise NotebookError("command_conflict", 409)
                return self.views.execution(document["notebookId"], eid)
            async with environment_gate(self.workspace):
                env = await self._ready(
                    document["environmentId"], document["environmentRevision"]
                )
                if env["spec"]["language"] != document["language"]:
                    raise NotebookError("notebook_language_mismatch", 409)
                self._budget(
                    new_kernel=agent
                    and self.db.run(
                        lambda c: (
                            (
                                k := current_kernel(
                                    c, document["notebookId"], document["language"]
                                )
                            )
                            is None
                            or k["state"] in {"released", "lost"}
                        )
                    )
                )
                if document["timeoutSeconds"] > self.limits["timeoutSeconds"]:
                    raise NotebookError("notebook_resource_limit", 409)
                if self.environment_guard is not None:
                    self.environment_guard.admission(document, eid, env)
                token = self.admission_environment.set(env)
                analysis_token = self.analysis_operation.set(analysis_operation)
                checkpoint_token = self.checkpoint_operation.set(checkpoint_operation)
                try:
                    await self.service.execute(
                        document["notebookId"],
                        document["cellId"],
                        document["commandId"],
                        document["expectedRevision"],
                        source=document["source"],
                        origin=document["origin"],
                        timeout_s=document["timeoutSeconds"],
                        semantic=document,
                    )
                except BaseException:
                    if self.environment_guard is not None:
                        self.environment_guard.rollback_uncommitted("admission", eid)
                        self.environment_guard.rollback_uncommitted(
                            "kernel", document["kernelInstanceId"]
                        )
                    raise
                finally:
                    self.admission_environment.reset(token)
                    self.analysis_operation.reset(analysis_token)
                    self.checkpoint_operation.reset(checkpoint_token)
                return self.views.execution(document["notebookId"], eid)

    def check_retry(self, c, eid, semantic):
        _, digest = command(semantic)
        row = c.execute(
            "SELECT digest FROM notebook_runtime_executions WHERE execution_id=?",
            (eid,),
        ).fetchone()
        if row is None or row[0] != digest:
            raise NotebookError("command_conflict", 409)

    def idle(self, c, nid):
        if c.execute(
            "SELECT 1 FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) "
            "JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE e.notebook_id=? "
            "AND k.state NOT IN ('released','lost') AND (e.status IN ('queued','running','outcome_unknown') "
            "OR json_extract(e.output_json,'$.event_stream_pending')=1) LIMIT 1",
            (nid,),
        ).fetchone():
            raise NotebookError("notebook_busy", 409)

    def claim(self, c, nid, eid, semantic):
        self.authorize()
        self.fence(c)
        document, digest = command(semantic)
        operation = self.analysis_operation.get()
        checkpoint = self.checkpoint_operation.get()
        if checkpoint is not None:
            self.checkpoints.claim(c, nid, eid, checkpoint)
        elif operation is not None:
            self.restores.require_clear(c, nid, document["language"])
            self.analysis.claim(c, nid, eid, operation)
        else:
            self.restores.require_clear(c, nid, document["language"])
            self.checkpoints.store.require_clear(c, nid, document["language"])
            if document["origin"] != "agent":
                self.analysis.store.run_guard(c, nid, document)
        if document["origin"] == "agent" and operation is None:
            pre = document["preconditions"]
            facts = self.requests.verify(
                c,
                pre["token"],
                pre["sessionId"],
                pre["requestId"],
                language=document["language"],
            )
            if facts["kernelInstanceId"] is None or facts["state"] in {
                "released",
                "lost",
            }:
                expected_nid = (
                    facts["notebookId"]
                    or hashlib.sha256(
                        ("notebook:" + pre["sessionId"]).encode()
                    ).hexdigest()
                )
                expected_instance = hashlib.sha256(
                    (
                        "kernel:"
                        + pre["sessionId"]
                        + ":"
                        + pre["requestId"]
                        + (
                            f":gen{facts['nextGeneration']}"
                            if facts["nextGeneration"] > 1
                            else ""
                        )
                        + (":r" if document["language"] == "r" else "")
                    ).encode()
                ).hexdigest()[:32]
                if (
                    nid != expected_nid
                    or document["kernelInstanceId"] != expected_instance
                    or document["generation"] != facts["nextGeneration"]
                ):
                    raise NotebookError("notebook_request_invalid", 409)
                env = self.admission_environment.get()
                if (
                    not env
                    or env["environmentId"] != facts["environmentId"]
                    or env["revision"] != facts["environmentRevision"]
                ):
                    raise NotebookError("environment_revision_conflict", 409)
                key, now = "nk" + expected_instance, time.time()
                c.execute(
                    "INSERT INTO notebook_runtime_kernels(instance_id,notebook_id,generation,session_key,environment_id,environment_revision,environment_json,maintenance_fence,state,process_json,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,0,'reserved',NULL,?,?)",
                    (
                        expected_instance,
                        nid,
                        facts["nextGeneration"],
                        key,
                        env["environmentId"],
                        env["revision"],
                        encoded(
                            {
                                **env,
                                "componentSourceDigest": self.source_digest,
                                "kernelConnectionRef": f".omicsclaw/kernel/{key}/runtime/kernel.json",
                            }
                        ).decode(),
                        now,
                        now,
                    ),
                )
                c.execute(
                    "UPDATE notebook_runtime_kernels SET language=? WHERE instance_id=?",
                    (document["language"], expected_instance),
                )
                c.execute(
                    "INSERT INTO notebook_bindings VALUES(?,?,?,?) ON CONFLICT(notebook_id) DO UPDATE SET kernel_session_id=CASE WHEN ?='python' THEN excluded.kernel_session_id ELSE notebook_bindings.kernel_session_id END",
                    (
                        nid,
                        pre["sessionId"],
                        key if document["language"] == "python" else "",
                        now,
                        document["language"],
                    ),
                )
                self.requests.provenance(c, "kernel", expected_instance)
            elif facts["notebookId"] != nid:
                raise NotebookError("notebook_request_invalid", 409)
        kernel = require_kernel(
            c,
            nid,
            document["kernelInstanceId"],
            document["generation"],
            document["language"],
        )
        if (
            kernel["environment_id"] != document["environmentId"]
            or kernel["environment_revision"] != document["environmentRevision"]
        ):
            raise NotebookError("environment_revision_conflict", 409)
        if kernel["process_json"] and not same_process(
            json.loads(kernel["process_json"])
        ):
            raise NotebookError("notebook_kernel_unavailable", 409)
        if document["origin"] == "user" and checkpoint is None:
            self.requests.takeover(c, nid)
        c.execute(
            "INSERT INTO notebook_runtime_executions VALUES(?,?,?,?,?,'pending',0,'pending',?)",
            (
                eid,
                digest,
                encoded(document).decode(),
                kernel["instance_id"],
                kernel["generation"],
                self.epoch,
            ),
        )
        self.requests.provenance(c, "execution", eid)
        sid = c.execute(
            "SELECT session_id FROM notebook_bindings WHERE notebook_id=?", (nid,)
        ).fetchone()[0]
        if checkpoint is None:
            self.helpers.capture(c, eid, sid)
        # This source update shares the original service's CAS transaction.
        c.execute(
            "UPDATE notebook_cells SET source=?,language=? WHERE notebook_id=? AND cell_id=? AND revision=?",
            (
                document["source"],
                document["language"],
                nid,
                document["cellId"],
                document["expectedRevision"],
            ),
        )
        bump(c, nid)

    def execution_target(self, c, eid):
        row = c.execute(
            "SELECT k.session_key,k.language FROM notebook_runtime_executions x JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE x.execution_id=?",
            (eid,),
        ).fetchone()
        if row is None:
            raise NotebookError("execution_not_found", 404)
        return row[0], row[1]

    def admission_idle(self, c, nid, semantic):
        # User runs queue behind the current cell; they do not interrupt it.
        if semantic["origin"] == "agent":
            operation = self.analysis_operation.get()
            self.analysis.inspection_idle(
                c, nid, operation["command"]["action"] if operation else None
            )
        elif c.execute(
            "SELECT 1 FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) "
            "WHERE e.notebook_id=? AND x.instance_id=? AND e.status='outcome_unknown' LIMIT 1",
            (nid, semantic["kernelInstanceId"]),
        ).fetchone():
            raise NotebookError("notebook_outcome_unknown", 409)

    def admission_committed(self, eid):
        if self.environment_guard is not None:
            self.environment_guard.committed(eid)

    def execution_committed(self, eid):
        if self.environment_guard is not None:
            self.environment_guard.finished(eid)

    def execution_finished(self, nid, eid):
        self.checkpoints.finished(eid)
        if self.running_notebooks.get(nid) == eid:
            self.running_notebooks.pop(nid, None)
        self.wake()

    def wake(self):
        self.wakeup.set()

    async def _schedule(self):
        while not self.closed:
            await self.wakeup.wait()
            self.wakeup.clear()
            try:
                self._sync_sandbox_grants()
                self.checkpoints.schedule()
                self.restores.schedule()
                rows = await self.db.arun(
                    lambda c: c.execute(
                        "SELECT e.notebook_id,e.execution_id,x.cancel_requested FROM notebook_executions e "
                        "JOIN notebook_runtime_executions x USING(execution_id) WHERE e.status='queued' "
                        "AND x.dispatch_state='pending' AND x.owner_epoch=? ORDER BY e.created_at LIMIT ?",
                        (self.epoch, self.limits["pending"]),
                    ).fetchall()
                )
                for row in rows:
                    if not self.db.run(
                        lambda c, row=row: self.checkpoints.allowed_dispatch(
                            c, row["notebook_id"], row["execution_id"]
                        )
                    ):
                        continue
                    if row["notebook_id"] in self.running_notebooks:
                        continue
                    if row["cancel_requested"]:
                        self.service.cancelling.add(row["execution_id"])
                    self.running_notebooks[row["notebook_id"]] = row["execution_id"]
                    self.service.dispatch(row["notebook_id"], row["execution_id"])
            except Exception:  # noqa: BLE001 - fail closed when owner evidence or durable writes fail
                self.durability_failed = True

    async def before_dispatch(self, nid, eid):
        document = self.db.run(
            lambda c: json.loads(
                c.execute(
                    "SELECT semantic_json FROM notebook_runtime_executions WHERE execution_id=?",
                    (eid,),
                ).fetchone()[0]
            )
        )
        async with environment_gate(self.workspace):
            await self._ready(
                document["environmentId"],
                document["environmentRevision"],
                read_reservation=self.environment_guard.key("admission", eid)
                if self.environment_guard
                else None,
            )

            if self.environment_guard is not None:
                self.environment_guard.dispatch(eid, document["kernelInstanceId"])

            def work(c):
                self.fence(c)
                if document["origin"] == "agent":
                    pre = document["preconditions"]
                    old = self.requests.decode(
                        pre["token"], pre["sessionId"], pre["requestId"]
                    )
                    current, _, _, _ = self.requests.facts(
                        c, pre["sessionId"], pre["requestId"]
                    )
                    if current["controlMode"] != "agent":
                        raise NotebookError("notebook_user_control", 409)
                    if (
                        any(
                            current["languages"][language] != old["languages"][language]
                            for language in ("python", "r")
                            if language != document["language"]
                        )
                        or any(
                            current[key] != old[key]
                            for key in ("stateSeq", "userWatermark", "helperRevision")
                        )
                        or any(
                            current["languages"][document["language"]][key]
                            != old["languages"][document["language"]][key]
                            for key in ("environmentId", "environmentRevision")
                        )
                    ):
                        raise NotebookError("notebook_request_stale", 409)
                kernel = require_kernel(
                    c,
                    nid,
                    document["kernelInstanceId"],
                    document["generation"],
                    document["language"],
                )
                row = c.execute(
                    "SELECT cancel_requested,dispatch_state FROM notebook_runtime_executions WHERE execution_id=?",
                    (eid,),
                ).fetchone()
                if row[0]:
                    raise asyncio.CancelledError()
                if row[1] != "pending":
                    raise NotebookError("notebook_outcome_unknown", 409)
                private = self.checkpoints.execution(c, eid)
                if private:
                    self.checkpoints.check_private(c, nid, private[0])
                else:
                    self.restores.require_clear(c, nid, document["language"])
                    self.checkpoints.store.require_clear(
                        c,
                        nid,
                        document["language"],
                        inspect=bool(
                            c.execute(
                                "SELECT 1 FROM notebook_analysis_operations WHERE execution_id=?",
                                (eid,),
                            ).fetchone()
                        ),
                    )
                    self.analysis.dispatch(c, nid, eid, document)
                    if not c.execute(
                        "SELECT 1 FROM notebook_analysis_operations WHERE execution_id=?",
                        (eid,),
                    ).fetchone():
                        self.checkpoints.store.mutation(
                            c, nid, document["language"], advance=True, execution_id=eid
                        )
                c.execute(
                    "UPDATE notebook_runtime_executions SET dispatch_state='dispatching' WHERE execution_id=?",
                    (eid,),
                )
                c.execute(
                    "UPDATE notebook_runtime_kernels SET state='busy',confirmed_at=? WHERE instance_id=?",
                    (time.time(), kernel["instance_id"]),
                )
                bump(c, nid)

            await asyncio.to_thread(self.service._transaction, work)

    async def _start_kernel(self, state, on_figure):
        async with environment_gate(self.workspace):
            row = self.db.run(
                lambda c: c.execute(
                    "SELECT * FROM notebook_runtime_kernels WHERE session_key=?",
                    (state.session_id,),
                ).fetchone()
            )
            if (
                row is None
                or row["state"] not in {"reserved", "busy"}
                or row["process_json"]
            ):
                raise NotebookError("notebook_kernel_unavailable", 409)
            env = await self._ready(
                row["environment_id"],
                row["environment_revision"],
                read_reservation=self.environment_guard.key(
                    "kernel", row["instance_id"]
                )
                if self.environment_guard
                else None,
            )
            if self.environment_guard is not None:
                self.environment_guard.start(row["instance_id"])
            kernel_root = private_directory(self.workspace / ".omicsclaw/kernel")
            scratch = private_directory(kernel_root / state.session_id)
            private_directory(scratch / "runtime")

            def register(pid):
                evidence = process_evidence(pid)
                if evidence is None:
                    raise NotebookError("notebook_kernel_unavailable", 409)

                def save(c):
                    self.fence(c)
                    require_kernel(
                        c, row["notebook_id"], row["instance_id"], row["generation"]
                    )
                    c.execute(
                        "UPDATE notebook_runtime_kernels SET process_json=?,confirmed_at=? WHERE instance_id=?",
                        (encoded(evidence).decode(), time.time(), row["instance_id"]),
                    )

                self.service._transaction(save)
                if self.environment_guard is not None:
                    self.environment_guard.process(row["instance_id"], evidence)

                def guard(candidate):
                    if candidate != pid or not same_process(evidence):
                        return False
                    try:
                        return self.db.run(
                            lambda c: self._signal_allowed(c, row, evidence)
                        )
                    except Exception:  # noqa: BLE001 - fail closed when owner evidence or durable writes fail
                        return False

                return guard

            from omicsclaw.entry.process_containment import SubreaperLifecycleWrapper

            kernel = SessionKernel(
                state.session_id,
                workspace=self.workspace,
                python=env["spec"]["executable"]
                if row["language"] == "python"
                else None,
                language=row["language"],
                python_isolated=row["language"] == "python",
                r_launch=RegisteredRLaunch(
                    env["spec"]["executable"], tuple(env["spec"]["libraryPaths"])
                )
                if row["language"] == "r"
                else None,
                limits=self.manager.limits,
                on_figure=on_figure,
                spawn_wrapper=(
                    SubreaperLifecycleWrapper(
                        private_directory(
                            self.workspace / ".omicsclaw/process-lifecycles"
                        ),
                        inner=self.sandbox.spawn_wrapper(host_pid_lifecycle=True),
                    )
                    if (
                        self.sandbox is not None
                        and self.sandbox.as_kernel_spawn_wrapper() is not None
                        and str(getattr(self.sandbox, "backend_name", "")) == "netns"
                        and hasattr(self.sandbox, "spawn_wrapper")
                    )
                    else (
                        self.sandbox.as_kernel_spawn_wrapper() if self.sandbox else None
                    )
                )
                or self.lifecycle_wrapper,
                env_scrubber=without_control_credentials,
                signal_guard_factory=register,
            )
            try:
                await asyncio.to_thread(kernel.start)
                if self.environment_guard is not None:
                    self.environment_guard.confirm_started(row["instance_id"])
                if row["language"] == "r":
                    required = set(env["spec"]["requiredPackages"]) | {
                        "IRkernel",
                        "jsonlite",
                    }
                    result = await asyncio.to_thread(
                        kernel.execute,
                        identity_code(required),
                        store_history=False,
                        timeout_s=15,
                    )
                    try:
                        actual = json.loads(result.stdout)
                        expected = env["observation"]
                        matches = (
                            result.status == "ok"
                            and isinstance(actual, dict)
                            and all(
                                actual.get(key) == expected.get(key)
                                for key in (
                                    "version",
                                    "prefix",
                                    "executable",
                                    "libraryPaths",
                                )
                            )
                            and actual.get("packages")
                            == {p: expected["packages"].get(p) for p in required}
                        )
                    except (ValueError, TypeError, KeyError):
                        matches = False
                    if not matches:
                        raise NotebookError("notebook_r_identity_mismatch", 409)
            except BaseException as error:
                await asyncio.to_thread(kernel.shutdown)
                if (
                    row["language"] == "r"
                    and isinstance(error, Exception)
                    and not isinstance(error, NotebookError)
                ):
                    raise NotebookError("notebook_r_start_failed", 503) from error
                raise
            return kernel

    def _signal_allowed(self, c, expected, evidence):
        self.fence(c)
        row = current_kernel(c, expected["notebook_id"], expected["language"])
        return bool(
            row
            and row["instance_id"] == expected["instance_id"]
            and row["generation"] == expected["generation"]
            and json.loads(row["process_json"] or "null") == evidence
        )

    def state_changed(self, c, nid, eid, status, output):
        self.fence(c)
        row = c.execute(
            "SELECT instance_id FROM notebook_runtime_executions WHERE execution_id=?",
            (eid,),
        ).fetchone()
        if status in TERMINAL:
            self.requests.changed(c, nid, eid)
            durability = (
                "failed"
                if output and output.get("durable_error")
                else "pending"
                if output and output.get("event_stream_pending")
                else "committed"
            )
            c.execute(
                "UPDATE notebook_runtime_executions SET dispatch_state='terminal',durability=? WHERE execution_id=?",
                (durability, eid),
            )
            kernel = c.execute(
                "SELECT * FROM notebook_runtime_kernels WHERE instance_id=?", (row[0],)
            ).fetchone()
            self.checkpoints.terminal(c, nid, eid, status, kernel)
            state = (
                "ready"
                if same_process(json.loads(kernel["process_json"] or "null"))
                else "reserved"
                if kernel["process_json"] is None
                else "lost"
            )
            c.execute(
                "UPDATE notebook_runtime_kernels SET state=?,confirmed_at=? WHERE instance_id=?",
                (state, time.time(), row[0]),
            )
        bump(c, nid)

    @staticmethod
    def prepare_output(output):
        def trim(value):
            if isinstance(value, str):
                data = value.encode()
                return (
                    value
                    if len(data) <= 8192
                    else data[:8192].decode(errors="ignore") + "\n[output truncated]"
                )
            if isinstance(value, dict):
                return {k: trim(v) for k, v in list(value.items())[:32]}
            if isinstance(value, list):
                return [trim(v) for v in value[:16]]
            return value

        result = trim({k: v for k, v in output.items() if k != "outputs"})
        if "outputs" in output:
            result["outputs"] = output["outputs"]
        if result != output:
            result["truncated"] = True
        return result

    def stream(self, session, eid, events):
        def failed():
            self.durability_failed = True

        stream = OwnerOutputStream(
            session, eid, events, failure=failed, total_bytes=self.limits["outputBytes"]
        )
        self.streams[eid] = stream
        return stream

    async def close_stream(self, eid, stream):
        await stream.close()
        self.streams.pop(eid, None)

    async def _heartbeat(self):
        while not self.closed:
            try:

                def renew(c):
                    self.fence(c)
                    c.execute(
                        "UPDATE notebook_owners SET lease_until=? WHERE owner_id=?",
                        (time.time() + 30, self.owner_id),
                    )
                    for kernel in c.execute(
                        "SELECT * FROM notebook_runtime_kernels WHERE state IN ('ready','busy','unconfirmed') LIMIT ?",
                        (MAX_KERNELS,),
                    ).fetchall():
                        if kernel["process_json"] and not same_process(
                            json.loads(kernel["process_json"])
                        ):
                            c.execute(
                                "UPDATE notebook_runtime_kernels SET state='lost',confirmed_at=? WHERE instance_id=?",
                                (time.time(), kernel["instance_id"]),
                            )
                            bump(c, kernel["notebook_id"])

                await self.db.arun(renew)
                for stream in tuple(self.streams.values()):
                    await stream.renew()
            except Exception:  # noqa: BLE001 - fail closed when owner evidence or durable writes fail
                self.durability_failed = True
            await asyncio.sleep(min(2, self.service.events.lease_s / 3))

    async def cancel(self, params):
        required = {
            "notebookId",
            "executionId",
            "commandId",
            "kernelInstanceId",
            "generation",
        }
        if not required <= set(params) <= required | {"language"} or params.get(
            "language", "python"
        ) not in {"python", "r"}:
            raise NotebookError("invalid_notebook_command")
        for name in required - {"generation"}:
            identifier(params[name])
        revision(params["generation"])
        async with self.operation_lock:

            def work(c):
                self.authorize()
                self.fence(c)
                digest, old = self._receipt(c, params["commandId"], params)
                if old:
                    return old, False
                nid, eid = params["notebookId"], params["executionId"]
                kernel = require_kernel(
                    c,
                    nid,
                    params["kernelInstanceId"],
                    params["generation"],
                    params.get("language"),
                )
                row = c.execute(
                    "SELECT x.instance_id,e.status,x.cancel_requested FROM notebook_runtime_executions x JOIN "
                    "notebook_executions e USING(execution_id) WHERE e.notebook_id=? AND e.execution_id=?",
                    (nid, eid),
                ).fetchone()
                if row is None or row[0] != kernel["instance_id"]:
                    raise NotebookError("notebook_generation_changed", 409)
                if self.checkpoints.execution(c, eid) and row[1] not in TERMINAL:
                    raise NotebookError("notebook_checkpoint_blocked", 409)
                if row[1] == "outcome_unknown":
                    raise NotebookError("notebook_outcome_unknown", 409)
                c.execute(
                    "UPDATE notebook_runtime_executions SET cancel_requested=1 WHERE execution_id=?",
                    (eid,),
                )
                result = {
                    "commandId": params["commandId"],
                    "notebookId": nid,
                    "executionId": eid,
                    "status": "cancel_requested" if row[1] not in TERMINAL else row[1],
                }
                c.execute(
                    "INSERT INTO notebook_runtime_commands VALUES(?,?,?)",
                    (params["commandId"], digest, encoded(result).decode()),
                )
                bump(c, nid)
                return result, row[1] not in TERMINAL and not row[2]

            result, active = self.service._transaction(work)
            if active:
                self.service.cancelling.add(params["executionId"])
                self.wake()
                self._own(
                    self._cancel_execution(params["notebookId"], params["executionId"])
                )
            return result

    async def _cancel_execution(self, nid, eid):
        for _ in range(100):
            if eid in self.service.tasks:
                await self.service.cancel(nid, eid)
                return
            if self.views.execution(nid, eid)["status"] in TERMINAL:
                return
            await asyncio.sleep(0.02)
        raise NotebookError("notebook_owner_unavailable", 503)

    async def release(self, params):
        required = {"notebookId", "commandId", "kernelInstanceId", "generation"}
        if not required <= set(params) <= required | {"language"} or params.get(
            "language", "python"
        ) not in {"python", "r"}:
            raise NotebookError("invalid_notebook_command")
        for name in required - {"generation"}:
            identifier(params[name])
        revision(params["generation"])
        async with self.operation_lock:
            self.authorize()
            digest, old = self.db.run(
                lambda c: self._receipt(c, params["commandId"], params)
            )
            if (
                old
                and old.get("status") == "released"
                and not self.db.run(
                    lambda c: self.restores.active(
                        c, params["notebookId"], params.get("language", "python")
                    )
                )
            ):
                return old
            row = self.db.run(
                lambda c: c.execute(
                    "SELECT * FROM notebook_runtime_kernels WHERE notebook_id=? AND instance_id=?",
                    (params["notebookId"], params["kernelInstanceId"]),
                ).fetchone()
            )
            if (
                row is None
                or row["instance_id"] != params["kernelInstanceId"]
                or row["generation"] != params["generation"]
                or (
                    params.get("language") is not None
                    and row["language"] != params["language"]
                )
                or self.db.run(
                    lambda c: current_kernel(c, params["notebookId"], row["language"])[
                        "instance_id"
                    ]
                )
                != row["instance_id"]
            ):
                raise NotebookError("notebook_generation_changed", 409)
            self.db.run(
                lambda c: self.checkpoints.store.require_clear(
                    c, params["notebookId"], row["language"]
                )
            )
            restore_release = self.db.run(
                lambda c: self.restores.release_allowed(
                    c, params["notebookId"], row["language"]
                )
            )
            if old is None:
                self.db.run(
                    lambda c: c.execute(
                        "INSERT INTO notebook_runtime_commands VALUES(?,?,?)",
                        (
                            params["commandId"],
                            digest,
                            encoded(
                                {
                                    "commandId": params["commandId"],
                                    "status": "releasing",
                                }
                            ).decode(),
                        ),
                    )
                )
            executions = self.db.run(
                lambda c: c.execute(
                    "SELECT e.execution_id FROM notebook_executions e JOIN "
                    "notebook_runtime_executions x USING(execution_id) WHERE x.instance_id=? AND e.status IN ('queued','running') LIMIT ?",
                    (row["instance_id"], MAX_PENDING),
                ).fetchall()
            )
            state = self.manager.states.get(
                (row["session_key"], row["language"], "local")
            )
            if restore_release and state and state.kernel:
                # Explicit abandonment of this consumed target permits a guarded
                # kill; ordinary cancellation still cannot escalate its loader.
                await asyncio.to_thread(state.kernel.kill)
            for item in executions:
                eid = item[0]
                self.service.cancelling.add(eid)
                self.wake()
                await self._cancel_execution(row["notebook_id"], eid)
            if state and state.kernel:
                async with state.lock:
                    await asyncio.to_thread(state.kernel.shutdown)
                    state.kernel = None
            else:
                evidence = json.loads(row["process_json"] or "null")

                # Recover a missed startup receipt while the original launcher
                # still exists. A subreaper must drain before it exits.
                def permitted(pid):
                    return bool(
                        evidence
                        and pid == evidence["pid"]
                        and same_process(evidence)
                        and self.db.run(
                            lambda c: self._signal_allowed(c, row, evidence)
                        )
                        and same_process(evidence)
                    )

                if same_process(evidence) and permitted(evidence["pid"]):
                    proof = (
                        self.environment_guard.release_evidence(
                            row["instance_id"], evidence
                        )
                        if self.environment_guard is not None
                        else None
                    )
                    if proof and proof["kind"] == "linux_subreaper":
                        from omicsclaw.entry.process_containment import (
                            SubreaperLifecycleWrapper,
                        )

                        wrapper = SubreaperLifecycleWrapper(
                            Path(proof["directory"]).parent
                        )
                        await asyncio.to_thread(
                            wrapper.terminate, evidence["pid"], permitted
                        )
                    elif (
                        restore_release
                        and self.environment_guard is not None
                        and proof is None
                    ):
                        raise NotebookError("notebook_checkpoint_stop_unconfirmed", 409)
                    elif permitted(evidence["pid"]):
                        os.kill(evidence["pid"], signal.SIGKILL)
            evidence = json.loads(row["process_json"] or "null")
            for _ in range(100):
                if not same_process(evidence):
                    break
                await asyncio.sleep(0.02)
            if same_process(evidence):
                raise NotebookError("notebook_release_unconfirmed", 409)

            if self.environment_guard is not None:
                self.environment_guard.released(row["instance_id"])

            def work(c):
                self.fence(c)
                restored_id = self.restores.released(
                    c, params["notebookId"], row["language"]
                )
                c.execute(
                    "UPDATE notebook_runtime_kernels SET state='released',confirmed_at=? WHERE instance_id=?",
                    (time.time(), row["instance_id"]),
                )
                bump(c, row["notebook_id"])
                result = {
                    "commandId": params["commandId"],
                    "kernelInstanceId": row["instance_id"],
                    "notebookId": params["notebookId"],
                    "generation": row["generation"],
                    "status": "released",
                }
                c.execute(
                    "UPDATE notebook_runtime_commands SET receipt_json=? WHERE command_id=?",
                    (encoded(result).decode(), params["commandId"]),
                )
                return result, restored_id

            result, restored_id = self.service._transaction(work)
            if restored_id:
                await asyncio.to_thread(
                    self.restores.cleanup, params["notebookId"], restored_id
                )
            return result

    def _figure(self, path, mime, session_id, job_id):
        digest, size = hash_and_size(Path(path))
        execution = self.db.run(
            lambda c: c.execute(
                "SELECT e.execution_id FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) "
                "JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE k.session_key=? AND e.status='running' "
                "ORDER BY e.created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        )
        aid = new_artifact_id()
        eid = execution[0] if execution else None
        snapshot_path = None
        try:
            figures_dir = private_directory(self.workspace / ".omicsclaw/notebook-figures")
            snap_target = figures_dir / f"{aid}.png"
            snap_target.write_bytes(Path(path).read_bytes())
            os.chmod(snap_target, 0o600)
            snapshot_path = str(snap_target)
        except OSError:
            snapshot_path = None
        meta = {
            "capture": "kernel",
            "executionId": eid,
        }
        if snapshot_path:
            meta["snapshotPath"] = snapshot_path
        scoped_job_id = f"{eid or job_id or 'notebook'}:{aid}"
        record = ArtifactRecord(
            id=aid,
            job_id=scoped_job_id,
            session_id=session_id,
            kind="figure",
            path=str(path),
            title=Path(path).stem,
            mime=mime,
            sha256=digest,
            size=size,
            produced_by="notebook_kernel",
            meta=meta,
            created_at=time.time(),
        )
        existing = self.artifacts.insert_artifact(record)
        return existing.id if existing else record.id

    async def close(self):
        if self.maintenance_service:
            await self.maintenance_service.close()
        self.closed = True
        for task in (self.scheduler, self.heartbeat):
            if task:
                task.cancel()
        await asyncio.gather(
            *(t for t in (self.scheduler, self.heartbeat) if t), return_exceptions=True
        )
        await self.restores.close()
        await self.checkpoints.close()
        await self.service.close()
        await self.manager.aclose()
        if self.environment_guard is not None:
            rows = self.db.run(
                lambda c: c.execute(
                    "SELECT instance_id,process_json FROM notebook_runtime_kernels WHERE state NOT IN ('released','lost')"
                ).fetchall()
            )
            for row in rows:
                if not same_process(json.loads(row["process_json"] or "null")):
                    self.db.run(
                        lambda c, instance=row["instance_id"]: c.execute(
                            "UPDATE notebook_runtime_kernels SET state='released' WHERE instance_id=?",
                            (instance,),
                        )
                    )
                    self.environment_guard.released(row["instance_id"])
        if self.sandbox:
            await self.sandbox.stop()
        self.artifact_db.close()
        self.registry_db.close()
        self.db.close()
