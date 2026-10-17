"""Each owner applies fixed maintenance work only to its own persisted kernels."""

from __future__ import annotations

import asyncio
import json
import time

from .environment_coordination import CoordinationError, encoded, overlaps
from .environment_scopes import registered_read_roots
from .maintenance_store import digest
from .notebook.security import environment_gate
from .notebook.store import bump, current_kernel, kernel_row


class OwnerMaintenanceMailbox:
    def __init__(self, service):
        self.service, self.runtime = service, service.runtime
        self.store, self.coordinator = service.store, service.coordinator

    async def poll(self):
        with self.coordinator.transaction() as c:
            rows = c.execute(
                "SELECT * FROM maintenance_actions WHERE workspace_id=? AND owner_id=? AND owner_epoch=? AND state='pending' LIMIT 8",
                (
                    self.runtime.identity["workspaceId"],
                    self.runtime.owner_id,
                    self.runtime.epoch,
                ),
            ).fetchall()
        for row in rows:
            with self.coordinator.transaction() as c:
                claimed = c.execute(
                    "UPDATE maintenance_actions SET state='running' WHERE action_id=? AND state='pending'",
                    (row["action_id"],),
                ).rowcount
            if not claimed:
                continue
            try:
                result = await self.execute(row)
                state = "done"
            except (ValueError, OSError) as error:
                reason = (
                    str(error)
                    if str(error).startswith("environment_maintenance_")
                    and len(str(error)) < 128
                    else "environment_maintenance_unconfirmed"
                )
                state, result = "failed", {"reason": reason}
            with self.coordinator.transaction() as c:
                c.execute(
                    "UPDATE maintenance_actions SET state=?,result_json=? WHERE action_id=? AND state='running'",
                    (state, encoded(result), row["action_id"]),
                )

    async def execute(self, action):
        document, private = self.store.read(action["maintenance_id"], scoped=False)
        data = json.loads(action["document_json"])
        kind = action["kind"]
        verification_targets = (
            private.get("verificationTargets", []) if kind == "verify" else []
        )
        row = next(
            (
                r
                for r in [
                    *private["impact"],
                    *private["restartImpact"],
                    *verification_targets,
                ]
                if r["reservation_id"] == data["reservationId"]
            ),
            None,
        )
        own = (
            data["reservationId"] == "maintenance-owner-" + self.runtime.owner_id
            and document["scope"] == self.runtime.identity
        )
        if not own and (
            row is None
            or (row["owner_id"], row["owner_epoch"], row["workspace_id"])
            != (
                self.runtime.owner_id,
                self.runtime.epoch,
                self.runtime.identity["workspaceId"],
            )
        ):
            raise CoordinationError("environment_maintenance_owner_changed")
        if kind in {"restart", "check_restart"}:
            return await self.restart(
                document, private, data["payload"], check_only=kind == "check_restart"
            )
        if kind == "verify":
            if document["state"] != "verifying":
                raise CoordinationError("environment_maintenance_invalid_state")
            return await self.verify(document, private)
        if kind not in {"stop", "interrupt"} or row is None or row["kind"] != "kernel":
            raise CoordinationError("environment_maintenance_unconfirmed")
        if document["state"] not in {
            "draining",
            "checkpoint_required",
            "checkpoint_barrier",
            "stopping_old_kernels",
            "blocked_unconfirmed",
        }:
            raise CoordinationError("environment_maintenance_invalid_state")
        identity = json.loads(row["identity_json"])["identity"]
        if not identity.get("notebookId"):
            raise CoordinationError("environment_maintenance_unconfirmed")
        current = self.runtime.db.run(
            lambda c: current_kernel(c, identity["notebookId"], identity["language"])
        )
        if (
            current is None
            or current["instance_id"] != row["resource_id"]
            or current["generation"] != identity["generation"]
        ):
            raise CoordinationError("environment_maintenance_restart_stale")
        if kind == "stop":
            actual = self.coordinator.get(row["reservation_id"])
            if actual["has_analysis"]:
                raise CoordinationError("environment_maintenance_checkpoint_required")
            await self.runtime.release(
                {
                    "notebookId": identity["notebookId"],
                    "language": identity["language"],
                    "kernelInstanceId": row["resource_id"],
                    "generation": identity["generation"],
                    "commandId": digest(["maintenance-stop", action["action_id"]]),
                }
            )
            if self.coordinator.get(row["reservation_id"])["state"] != "closed":
                raise CoordinationError("environment_maintenance_unconfirmed")
            return {"closed": True}
        if not document.get("interruptRequested"):
            raise CoordinationError("environment_maintenance_invalid_state")
        executions = self.runtime.db.run(
            lambda c: c.execute(
                "SELECT e.execution_id FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) WHERE x.instance_id=? AND e.status IN ('queued','running') LIMIT ?",
                (row["resource_id"], self.runtime.limits["pending"]),
            ).fetchall()
        )
        for execution in executions:
            await self.runtime.cancel(
                {
                    "notebookId": identity["notebookId"],
                    "language": identity["language"],
                    "kernelInstanceId": row["resource_id"],
                    "generation": identity["generation"],
                    "executionId": execution[0],
                    "commandId": digest(
                        ["maintenance-interrupt", action["action_id"], execution[0]]
                    ),
                }
            )
        return {"interrupted": len(executions)}

    async def verify(self, document, private):
        registry = self.runtime.registry
        ids = registry.database.run(
            lambda c: [
                r[0]
                for r in c.execute(
                    "SELECT environment_id FROM registered_environments LIMIT 64"
                )
            ]
        )
        selected = []
        for eid in ids:
            row = registry.get(eid)
            if row["observation"] and any(
                overlaps(a, b)
                for a in registered_read_roots(row)
                for b in document["plan"]["writeRoots"]
            ):
                selected.append(eid)
        result = {}
        fences = [document["maintenanceId"], *private["damageEpochs"]]
        with registry.read_guard.access(fences):
            async with environment_gate(self.runtime.workspace):
                for eid in selected:
                    observed = await registry._preflight(eid)
                    if observed["status"] not in {"ready", "changed"}:
                        raise CoordinationError("environment_maintenance_needs_repair")
                    accepted = registry._accept(eid, observed["revision"])
                    result[eid] = {
                        "environmentId": eid,
                        "revision": accepted["revision"],
                        "fingerprint": accepted["fingerprint"],
                    }
        return result

    async def restart(self, document, private, payload, *, check_only):
        from .maintenance_worker import MaintenanceWorker

        MaintenanceWorker(self.service).check_restart_epoch(document, private)
        selected = next(
            (
                r
                for r in private.get("restartRows", [])
                if r["restartId"] == payload.get("restartId")
            ),
            None,
        )
        if (
            selected is None
            or selected["workspaceId"] != self.runtime.identity["workspaceId"]
        ):
            raise CoordinationError("environment_maintenance_not_found")
        expected = selected["environment"]
        current_env = self.runtime.registry.get(expected["environmentId"])
        if (
            current_env["status"] != "ready"
            or current_env["acceptedRevision"] != expected["revision"]
            or current_env["fingerprint"] != expected["fingerprint"]
        ):
            raise CoordinationError("environment_maintenance_restart_stale")
        command_id = digest(["maintenance-bind", selected["restartId"]])
        current = self.runtime.db.run(
            lambda c: current_kernel(c, selected["notebookId"], selected["language"])
        )
        receipt = self.runtime.db.run(
            lambda c: c.execute(
                "SELECT receipt_json FROM notebook_runtime_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
        )
        bound = json.loads(receipt[0])["kernel"] if receipt else None
        permitted = (
            (selected["previousKernelInstanceId"], selected["previousGeneration"])
            if bound is None
            else (bound["kernelInstanceId"], bound["generation"])
        )
        if (
            current is None
            or (current["instance_id"], current["generation"]) != permitted
            or (bound is None and current["state"] not in {"released", "lost"})
            or (bound is not None and current["state"] not in {"reserved", "ready"})
        ):
            raise CoordinationError("environment_maintenance_restart_stale")
        if check_only:
            return {"current": True}
        if bound is None:
            session = self.runtime.db.run(
                lambda c: c.execute(
                    "SELECT session_id FROM notebook_bindings WHERE notebook_id=?",
                    (selected["notebookId"],),
                ).fetchone()
            )
            if session is None:
                raise CoordinationError("environment_maintenance_restart_stale")
            await self.runtime.bind(
                {
                    "notebookId": selected["notebookId"],
                    "sessionId": session[0],
                    "language": selected["language"],
                    "commandId": command_id,
                    "environmentId": expected["environmentId"],
                    "environmentRevision": expected["revision"],
                }
            )
            current = self.runtime.db.run(
                lambda c: current_kernel(
                    c, selected["notebookId"], selected["language"]
                )
            )
        state = self.runtime.manager.state_for(
            current["session_key"], language=current["language"]
        )
        async with state.lock:
            if state.kernel is None:
                await self.runtime.manager._start_locked(state)
            # A hidden startup check does not execute historical user cells.
            if not await asyncio.to_thread(state.kernel.is_alive):
                raise CoordinationError("environment_maintenance_restart_failed")

            def ready(c):
                self.runtime.fence(c)
                latest = current_kernel(c, selected["notebookId"], selected["language"])
                if latest["instance_id"] != current["instance_id"]:
                    raise CoordinationError("environment_maintenance_restart_stale")
                c.execute(
                    "UPDATE notebook_runtime_kernels SET state='ready',maintenance_fence=?,confirmed_at=? WHERE instance_id=?",
                    (private["committedEpoch"], time.time(), current["instance_id"]),
                )
                bump(c, selected["notebookId"])
                return kernel_row(
                    current_kernel(c, selected["notebookId"], selected["language"])
                )

            kernel = self.runtime.service._transaction(ready)
        return {
            "kernelInstanceId": kernel["kernelInstanceId"],
            "generation": kernel["generation"],
            "status": "ready",
            "reason": "",
        }
