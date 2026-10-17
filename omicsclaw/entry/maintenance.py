"""Owner-held approval and admission for fixed offline environment maintenance."""

from __future__ import annotations

import asyncio
import json
import uuid

from omicsclaw.tools.context import ApprovalDecision, ApprovalRequest

from .approval import ApprovalBroker
from .environment_coordination import CoordinationError, encoded, validate_roots
from .environment_scopes import registered_read_roots
from .maintenance_adapter import prepare_plan
from .maintenance_artifacts import verify_artifacts
from .maintenance_store import TERMINAL, MaintenanceStore, digest, key
from .notebook.security import private_directory, same_process
from .process_containment import contained_exit
from .stream import TurnStream


class MaintenanceService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.guard = runtime.environment_guard
        self.coordinator = self.guard.coordinator
        self.store = MaintenanceStore(
            self.coordinator, runtime.identity, self.guard.owner
        )
        self.directory = private_directory(
            runtime.workspace / ".omicsclaw/environment-maintenance"
        )
        self.lock = asyncio.Lock()
        self.approvals = {}
        self.tasks = {}
        self.work_tasks = set()
        self.poller = None
        self.closed = False

    def spawn(self, coroutine):
        # The accepted journal, rather than a live controller lease, authorizes work.
        token = self.runtime.authorization.set(None)
        try:
            return self.runtime._own(coroutine)
        finally:
            self.runtime.authorization.reset(token)

    def start_work(self, mid, coroutine):
        task = self.spawn(coroutine)
        self.tasks[mid] = task
        self.work_tasks.add(task)
        task.add_done_callback(self.work_tasks.discard)
        return task

    async def start(self):
        with self.coordinator.transaction() as c:
            rows = c.execute(
                "SELECT * FROM maintenance_records WHERE workspace_id=?",
                (self.runtime.identity["workspaceId"],),
            ).fetchall()
            pending = []
            for row in rows:
                document = json.loads(row["document_json"])
                if document["state"] in {
                    "completed",
                    "cancelled_before_mutation",
                    "plan_stale",
                }:
                    continue
                if row["owner_id"] != self.runtime.owner_id and same_process(
                    json.loads(row["owner_process_json"])
                ):
                    raise CoordinationError("environment_maintenance_owner_alive")
                c.execute(
                    "UPDATE maintenance_records SET owner_id=?,owner_epoch=?,owner_process_json=? WHERE maintenance_id=?",
                    (
                        self.runtime.owner_id,
                        self.runtime.epoch,
                        encoded(self.guard.owner["process"]),
                        document["maintenanceId"],
                    ),
                )
                if document["state"] in {"needs_repair", "restart_required"}:
                    continue
                if document["state"] == "awaiting_approval":
                    pending.append(document["maintenanceId"])
                    continue
                if (
                    document.get("result", {}).get("environmentVerified")
                    if document.get("result")
                    else False
                ):
                    document["state"] = "restart_required"
                elif document["writeStarted"]:
                    document["state"] = "needs_repair"
                    c.execute(
                        "UPDATE maintenance_fences SET state='needs_repair' WHERE maintenance_id=?",
                        (document["maintenanceId"],),
                    )
                else:
                    document["state"] = "cancelled_before_mutation"
                    # Only this operation's own pre-write fence may be closed.
                    c.execute(
                        "UPDATE maintenance_fences SET state='closed' WHERE maintenance_id=? AND state!='needs_repair'",
                        (document["maintenanceId"],),
                    )
                document["approval"] = None
                self.store.write(c, document, event={"kind": "owner_recovered"})
        for mid in pending:
            await self.offer(mid)
        self.poller = self.spawn(self.poll())

    async def close(self):
        self.closed = True
        for broker, waiter in self.approvals.values():
            broker.abandon()
        if self.poller:
            self.poller.cancel()
            await asyncio.gather(self.poller, return_exceptions=True)
        tasks = set(self.tasks.values()) | self.work_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def poll(self):
        from .maintenance_worker import MaintenanceWorker

        worker = MaintenanceWorker(self)
        while not self.closed:
            await worker.mailbox()
            await asyncio.sleep(0.1)

    async def offer(self, mid):
        document, _ = self.store.read(mid)
        stream = TurnStream(
            "environment-maintenance", mid + "-" + uuid.uuid4().hex, ring_size=8
        )
        broker = ApprovalBroker(stream, timeout_s=300)
        waiter = self.spawn(
            broker(
                ApprovalRequest(
                    tool_name="environment_maintenance",
                    arguments=encoded(
                        {
                            "maintenanceId": mid,
                            "planRevision": document["planRevision"],
                            "planDigest": document["planDigest"],
                        }
                    ),
                    reason="Install only the fixed local artifacts into the reviewed environment.",
                    ask_every_time=True,
                )
            )
        )
        await asyncio.sleep(0)
        ids = broker.pending()
        if len(ids) != 1:
            waiter.cancel()
            raise CoordinationError("environment_maintenance_approval_expired")
        self.approvals[mid] = broker, waiter
        self.store.update(
            mid,
            approval={
                "requestId": ids[0],
                "planRevision": document["planRevision"],
                "planDigest": document["planDigest"],
                "askEveryTime": True,
            },
            event={"kind": "approval_requested"},
        )
        self.spawn(self.expire(mid, waiter))

    async def expire(self, mid, waiter):
        decision = await asyncio.shield(waiter)
        if decision.approved or self.closed:
            return
        async with self.lock:
            document, _ = self.store.read(mid)
            if document["state"] == "awaiting_approval":
                self.store.update(
                    mid,
                    state="cancelled_before_mutation",
                    approval=None,
                    event={"kind": "approval_expired"},
                )

    def exact_plan(self, document, params):
        if type(params.get("planRevision")) is not int or not isinstance(
            params.get("planDigest"), str
        ):
            raise CoordinationError("environment_maintenance_invalid_request")
        if (
            params.get("planRevision") != document["planRevision"]
            or params.get("planDigest") != document["planDigest"]
        ):
            raise CoordinationError("environment_maintenance_plan_stale")

    async def call(self, action, params):
        if not isinstance(action, str) or action not in {
            "get",
            "list",
            "impact",
            "events",
            "receipt",
            "plan",
            "repair",
            "approve",
            "cancel",
            "interrupt",
            "restart",
        }:
            raise CoordinationError("environment_maintenance_invalid_request")
        if action in {"get", "list", "impact", "events", "receipt"}:
            if action == "get":
                if set(params) != {"maintenanceId"}:
                    raise CoordinationError("environment_maintenance_invalid_request")
                return {"maintenance": self.store.public(params["maintenanceId"])}
            return (
                self.store.receipt(params)
                if action == "receipt"
                else self.store.page(action, params)
            )
        async with self.lock:
            self.runtime.authorize()
            old = self.store.old(action, params)
            if old:
                return old
            if action in {"plan", "repair"}:
                return await self.plan(action, params)
            if action == "approve":
                return await self.approve(params)
            if action in {"cancel", "interrupt", "restart"}:
                return await self.action(action, params)
            raise CoordinationError("environment_maintenance_invalid_request")

    async def observe(self, environment_id, fences=()):
        with self.runtime.registry.read_guard.access(fences):
            return await self.runtime.registry.preflight(environment_id)

    async def verify_baseline(self, mid, *, installing=False):
        document, private = self.store.read(mid)
        fences = ([mid] if installing else []) + list(private["damageEpochs"])
        observed = await self.observe(document["environmentId"], fences)
        expected = private["environment"]
        if (
            observed["fingerprint"] != expected["fingerprint"]
            or observed["revision"] != expected["revision"]
        ):
            raise CoordinationError("environment_maintenance_plan_stale")
        return observed

    async def plan(self, action, params):
        fields = {"commandId", "environmentId", "environmentRevision", "operation"}
        if set(params) != fields | ({"repairOf"} if action == "repair" else set()):
            raise CoordinationError("environment_maintenance_invalid_request")
        before = self.runtime.registry.get(params["environmentId"])
        repair = None
        damage_epochs = {}
        if action == "repair":
            original, original_private = self.store.read(params["repairOf"])
            if (
                original["state"] != "needs_repair"
                or original["environmentId"] != before["environmentId"]
            ):
                raise CoordinationError("environment_maintenance_needs_repair")
            active = original_private.get("activeProcess")
            if active is None or not contained_exit(
                active.get("proof"), active.get("birth")
            ):
                raise CoordinationError("environment_maintenance_unconfirmed")
            repair = original["maintenanceId"]
            before = original_private["environment"]
            damage_epochs = dict(original_private["damageEpochs"])
            with self.coordinator.transaction() as c:
                original_fence = c.execute(
                    "SELECT fence_seq,state FROM maintenance_fences WHERE maintenance_id=?",
                    (repair,),
                ).fetchone()
                if original_fence is None or original_fence["state"] != "needs_repair":
                    raise CoordinationError("environment_maintenance_plan_stale")
                damage_epochs[repair] = original_fence["fence_seq"]
                for identity, epoch in damage_epochs.items():
                    fence = c.execute(
                        "SELECT fence_seq,state FROM maintenance_fences WHERE maintenance_id=?",
                        (identity,),
                    ).fetchone()
                    if fence is None or (fence["fence_seq"], fence["state"]) != (
                        epoch,
                        "needs_repair",
                    ):
                        raise CoordinationError("environment_maintenance_plan_stale")
        if (
            type(params["environmentRevision"]) is not int
            or params["environmentRevision"] != before["acceptedRevision"]
            or (not repair and before["status"] != "ready")
        ):
            raise CoordinationError("environment_revision_conflict")
        before = await self.observe(before["environmentId"], damage_epochs)
        if not repair and (
            before["status"] != "ready"
            or before["acceptedRevision"] != params["environmentRevision"]
        ):
            raise CoordinationError("environment_maintenance_revision_conflict")
        mid = uuid.uuid4().hex
        directory = private_directory(self.directory / mid)
        rid = "maintenance-plan-" + mid
        roots = registered_read_roots(before)
        self.coordinator.reserve(
            {
                "reservationId": rid,
                "ownerId": self.runtime.owner_id,
                "ownerEpoch": self.runtime.epoch,
                "workspaceId": self.runtime.identity["workspaceId"],
                "workspacePath": str(self.runtime.workspace),
                "kind": "probe",
                "resourceId": rid,
                "readRoots": roots,
                "identity": {
                    "ownerProcess": self.guard.owner["process"],
                    "maintenanceFences": list(damage_epochs),
                },
            },
            allowed_fences=tuple(damage_epochs),
        )
        self.coordinator.update(rid, self.guard.owner, committed=True)

        def process_started(birth, proof):
            self.coordinator.update(rid, self.guard.owner, process=birth)
            if self.coordinator.confirm_containment(rid, self.guard.owner) != proof:
                raise CoordinationError("environment_maintenance_unconfirmed")

        try:
            plan, private = await prepare_plan(
                before,
                params["operation"],
                directory,
                config=self.runtime.config,
                on_process=process_started,
                launch_intent=lambda: self.coordinator.starting_probe(
                    rid, self.guard.owner
                ),
                before_probe=lambda: self.coordinator.check(rid, self.guard.owner),
            )
        finally:
            self.coordinator.close(rid, self.guard.owner, local_terminal=True)
        observed = await self.observe(before["environmentId"], damage_epochs)
        if observed["fingerprint"] != before["fingerprint"]:
            raise CoordinationError("environment_maintenance_plan_stale")
        self.runtime.authorize()
        self.coordinator.reap_dead_controls()
        self.coordinator.reap_contained()
        impact = self.coordinator.impact(plan["writeRoots"])
        if len(encoded(impact).encode()) > 4 * 1024 * 1024:
            raise CoordinationError("environment_maintenance_resource_limit")
        restart_impact = (
            original_private.get("restartImpact", original_private["impact"])
            if repair
            else impact
        )
        displayed = {row["reservation_id"]: row for row in impact}
        for row in restart_impact:
            if row["kind"] == "kernel" and row["reservation_id"] not in displayed:
                displayed[row["reservation_id"]] = self.coordinator.get(
                    row["reservation_id"]
                )
        plan.update(
            impactDigest=self.coordinator.impact_digest(impact),
            impactCount=len(displayed),
            checkpoint={
                "state": "required"
                if any(r["kind"] == "kernel" and r["has_analysis"] for r in impact)
                else "not_required",
                "reason": "checkpoint_required"
                if any(r["has_analysis"] for r in impact)
                else "",
            },
            controlEnvironmentConflict=any(r["kind"] == "control" for r in impact),
        )
        if sum(row["kind"] == "kernel" for row in restart_impact) > 64:
            raise CoordinationError("environment_maintenance_resource_limit")
        from .maintenance_worker import MaintenanceWorker

        verification_targets = MaintenanceWorker(self).plan_verification_targets(
            restart_impact
        )
        private.update(
            directory=str(directory),
            impact=impact,
            damageEpochs=damage_epochs,
            restartImpact=restart_impact,
            verificationTargets=verification_targets,
        )
        document = {
            "maintenanceId": mid,
            "scope": self.runtime.identity,
            "planRevision": 1,
            "planDigest": digest(
                [plan, repair, damage_epochs, restart_impact, verification_targets]
            ),
            "statusSeq": 0,
            "state": "awaiting_approval",
            "language": before["spec"]["language"],
            "environmentId": before["environmentId"],
            "environmentRevision": before["acceptedRevision"],
            "plan": plan,
            "approval": None,
            "blockers": [],
            "writeStarted": False,
            "result": None,
            "repairOf": repair,
        }
        receipt = self.store.create(
            document, private, list(displayed.values()), action, params
        )
        await self.offer(mid)
        return {"maintenance": self.store.public(mid), "receipt": receipt}

    async def approve(self, params):
        fields = {
            "maintenanceId",
            "commandId",
            "planRevision",
            "planDigest",
            "approvalRequestId",
            "approved",
        }
        if set(params) != fields or params["approved"] is not True:
            raise CoordinationError("environment_maintenance_invalid_request")
        mid = params["maintenanceId"]
        key(params["approvalRequestId"])
        document, private = self.store.read(mid)
        self.exact_plan(document, params)
        offered = self.approvals.get(mid)
        if (
            document["state"] != "awaiting_approval"
            or offered is None
            or document["approval"]["requestId"] != params["approvalRequestId"]
        ):
            raise CoordinationError("environment_maintenance_approval_expired")
        try:
            from .maintenance_worker import MaintenanceWorker

            MaintenanceWorker(self).verification_targets(private)
            await self.verify_baseline(mid)
        except (ValueError, OSError):
            offered[0].abandon()
            self.store.update(
                mid,
                state="plan_stale",
                approval=None,
                event={"kind": "environment_changed"},
            )
            raise CoordinationError("environment_maintenance_plan_stale") from None
        verify_artifacts(private["artifacts"])
        roots = validate_roots(document["plan"]["writeRoots"], {"install_write"})
        self.runtime.authorize()
        broker, _waiter = offered
        # No await separates broker settlement from the global impact CAS and journal.
        if not broker.settle(
            params["approvalRequestId"], ApprovalDecision(True, scope="once")
        ):
            raise CoordinationError("environment_maintenance_approval_expired")
        try:
            with self.coordinator.transaction() as c:
                conflicts = list(self.coordinator._conflicts(c, roots))
                if any(
                    r["state"] != "needs_repair"
                    or r["fence_seq"]
                    != private["damageEpochs"].get(r["maintenance_id"])
                    for r in conflicts
                ) or {r["maintenance_id"] for r in conflicts} != set(
                    private["damageEpochs"]
                ):
                    raise CoordinationError("environment_maintenance_scope_busy")
                impact = self.coordinator._impact(c, roots, limit=4097)
                if (
                    self.coordinator.impact_digest(impact)
                    != document["plan"]["impactDigest"]
                ):
                    raise CoordinationError("environment_maintenance_plan_stale")
                if any(r["kind"] == "control" for r in impact):
                    raise CoordinationError(
                        "environment_maintenance_control_environment"
                    )
                c.execute(
                    "INSERT INTO maintenance_fences VALUES(?,?,'fencing',?,?)",
                    (
                        mid,
                        encoded(roots),
                        self.coordinator.sequence(c),
                        encoded(self.guard.owner),
                    ),
                )
                document.update(state="approved", approval=None)
                self.store.write(
                    c,
                    document,
                    event={
                        "kind": "approved",
                        "impactDigest": document["plan"]["impactDigest"],
                    },
                )
                receipt = self.store.accept(c, document, "approve", params)
        except CoordinationError:
            self.store.update(
                mid,
                state="plan_stale",
                approval=None,
                event={"kind": "approval_rejected"},
            )
            raise
        from .maintenance_worker import MaintenanceWorker

        self.start_work(mid, MaintenanceWorker(self).run(mid))
        return {"maintenance": self.store.public(mid), "receipt": receipt}

    async def action(self, action, params):
        fields = {"maintenanceId", "commandId"}
        fields |= (
            {"restartId", "notebookId", "language", "expectedGeneration"}
            if action == "restart"
            else {"confirmation", "planRevision", "planDigest"}
        )
        if set(params) != fields or (
            action != "restart"
            and params["confirmation"]
            != {
                "cancel": "cancel_maintenance",
                "interrupt": "interrupt_affected_computations",
            }[action]
        ):
            raise CoordinationError("environment_maintenance_invalid_request")
        document, private = self.store.read(params["maintenanceId"])
        if action != "restart":
            self.exact_plan(document, params)
        from .maintenance_worker import MaintenanceWorker

        worker = MaintenanceWorker(self)
        if action == "restart":
            if (
                type(params["expectedGeneration"]) is not int
                or params["expectedGeneration"] < 1
                or params["language"] not in ("python", "r")
            ):
                raise CoordinationError("environment_maintenance_invalid_request")
            key(params["restartId"])
            key(params["notebookId"])
            selected = next(
                (
                    row
                    for row in private.get("restartRows", [])
                    if row["restartId"] == params["restartId"]
                ),
                None,
            )
            if selected is None or (
                params["notebookId"],
                params["language"],
                params["expectedGeneration"],
            ) != (
                selected["notebookId"],
                selected["language"],
                selected["previousGeneration"],
            ):
                raise CoordinationError("environment_maintenance_restart_stale")
            await worker.check_restart(document, private, key(params["restartId"]))
        elif (
            action == "interrupt"
            and document["state"]
            not in {
                "draining",
                "checkpoint_required",
                "checkpoint_failed",
                "blocked_unconfirmed",
            }
            or action == "cancel"
            and document["state"] in TERMINAL
        ):
            raise CoordinationError("environment_maintenance_invalid_state")
        self.runtime.authorize()
        with self.coordinator.transaction() as c:
            current = json.loads(
                self.store.row(c, document["maintenanceId"])["document_json"]
            )
            if action == "cancel" and (
                current["state"] in TERMINAL
                or (current.get("result") or {}).get("environmentVerified")
            ):
                raise CoordinationError("environment_maintenance_invalid_state")
            current[action + "Requested"] = params.get("restartId", True)
            self.store.write(c, current, event={"kind": action + "_requested"})
            receipt = self.store.accept(c, current, action, params)
        if action == "cancel" and document["state"] == "awaiting_approval":
            self.approvals[document["maintenanceId"]][0].abandon()
        elif action == "cancel" and document["writeStarted"]:
            task = self.tasks.get(document["maintenanceId"])
            if task and not task.done():
                task.cancel()
        elif action == "restart":
            self.start_work(
                document["maintenanceId"],
                worker.retry_restart(document["maintenanceId"], params["restartId"]),
            )
        return {
            "maintenance": self.store.public(document["maintenanceId"]),
            "receipt": receipt,
        }
