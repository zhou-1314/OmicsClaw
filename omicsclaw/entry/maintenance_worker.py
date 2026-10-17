"""Accepted maintenance work outlives observers and advances only on durable evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .environment_coordination import CoordinationError, encoded, validate_roots
from .maintenance_adapter import install_plan, verify_plan
from .maintenance_store import digest, impact_row
from .notebook.security import same_process


class MaintenanceWorker:
    def __init__(self, service):
        self.service = service
        self.runtime = service.runtime
        self.store = service.store
        self.coordinator = service.coordinator

    def fence(self, mid, *, state=None):
        with self.coordinator.transaction() as c:
            document = json.loads(self.store.row(c, mid)["document_json"])
            row = c.execute(
                "SELECT * FROM maintenance_fences WHERE maintenance_id=?", (mid,)
            ).fetchone()
            if row is None or row["state"] in {"closed", "needs_repair"}:
                raise CoordinationError("environment_maintenance_unconfirmed")
            owner = json.loads(row["owner_json"])
            if (owner["ownerId"], owner["ownerEpoch"]) != (
                self.runtime.owner_id,
                self.runtime.epoch,
            ):
                raise CoordinationError("environment_maintenance_owner_changed")
            if state is not None:
                c.execute(
                    "UPDATE maintenance_fences SET state=? WHERE maintenance_id=?",
                    (state, mid),
                )
            return document, row

    def stage(self, mid, state, **values):
        self.fence(mid, state=state)
        return self.store.update(
            mid, state=state, event={"kind": "stage", "phase": state}, **values
        )

    def cancelled(self, mid):
        document, _private = self.store.read(mid)
        if not document.get("cancelRequested"):
            return False
        if document["writeStarted"]:
            self.damage(mid, "environment_maintenance_cancelled_after_mutation")
        else:
            with self.coordinator.transaction() as c:
                c.execute(
                    "UPDATE maintenance_fences SET state='closed' WHERE maintenance_id=? AND state!='needs_repair'",
                    (mid,),
                )
                document.update(
                    state="cancelled_before_mutation", blockers=[], approval=None
                )
                self.store.write(
                    c, document, event={"kind": "cancelled_before_mutation"}
                )
        return True

    def damage(self, mid, reason):
        with self.coordinator.transaction() as c:
            row = self.store.row(c, mid)
            document = json.loads(row["document_json"])
            if (
                document.get("result", {}).get("environmentVerified")
                if document.get("result")
                else False
            ):
                document["state"] = "restart_required"
            else:
                document["state"] = "needs_repair"
                c.execute(
                    "UPDATE maintenance_fences SET state='needs_repair' WHERE maintenance_id=?",
                    (mid,),
                )
            document["blockers"] = [{"reason": reason, "maintenanceId": mid}]
            self.store.write(c, document, event={"kind": "failed", "reason": reason})

    def process(self, mid, phase, birth, proof):
        self.fence(mid)
        self.store.private_update(
            mid, activeProcess={"phase": phase, "birth": birth, "proof": proof}
        )

    def result(self, mid, phase, result):
        self.store.update(
            mid,
            event={
                "kind": "process_result",
                "phase": phase,
                **{
                    key: result[key]
                    for key in (
                        "process",
                        "exitCode",
                        "exitConfirmed",
                        "outputTruncated",
                    )
                },
            },
        )
        text = result["output"]
        for index, offset in enumerate(range(0, len(text), 512)):
            self.store.update(
                mid,
                event={
                    "kind": "process_output",
                    "phase": phase,
                    "chunkIndex": index,
                    "text": text[offset : offset + 512],
                    "truncated": result["outputTruncated"],
                },
            )

    def impact(self, document):
        self.coordinator.reap_dead_controls()
        self.coordinator.reap_unsent_jobs()
        self.coordinator.reap_finished_jobs()
        return self.coordinator.impact(document["plan"]["writeRoots"])

    async def run(self, mid):
        try:
            document, private = self.store.read(mid)
            self.stage(mid, "draining")
            while True:
                if self.cancelled(mid):
                    return
                document, private = self.store.read(mid)
                current = self.impact(document)
                allowed = {row["reservation_id"] for row in private["impact"]}
                if any(row["reservation_id"] not in allowed for row in current):
                    raise CoordinationError("environment_maintenance_unconfirmed")
                busy = [row for row in current if row["kind"] != "kernel"]
                if document.get("interruptRequested"):
                    await self.interrupt(mid, private["impact"])
                if not busy:
                    break
                reason = (
                    "environment_maintenance_unconfirmed"
                    if any(
                        row["uncertain_exit"]
                        or not same_process(
                            json.loads(row["identity_json"])["identity"].get(
                                "ownerProcess"
                            )
                        )
                        for row in busy
                    )
                    else "environment_maintenance_draining"
                )
                self.store.update(
                    mid,
                    blockers=[
                        {**impact_row(row), "reason": reason} for row in busy[:40]
                    ],
                )
                await asyncio.sleep(0.1)
            self.stage(mid, "checkpoint_barrier", blockers=[])
            while True:
                if self.cancelled(mid):
                    return
                document, private = self.store.read(mid)
                current = self.impact(document)
                analysis = [
                    row
                    for row in current
                    if row["kind"] == "kernel" and row["has_analysis"]
                ]
                if not analysis and document["state"] != "checkpoint_required":
                    break
                if document.get("interruptRequested"):
                    await self.interrupt(mid, private["impact"])
                if document["state"] != "checkpoint_required":
                    self.stage(
                        mid,
                        "checkpoint_required",
                        blockers=[
                            {**impact_row(row), "reason": "checkpoint_required"}
                            for row in analysis[:40]
                        ],
                    )
                # S4 supplies checkpointing. Releasing memory requires a fresh plan.
                await asyncio.sleep(0.1)
            self.stage(mid, "stopping_old_kernels", blockers=[])
            for row in private["impact"]:
                if row["kind"] != "kernel":
                    continue
                if self.cancelled(mid):
                    return
                if self.coordinator.get(row["reservation_id"])["state"] == "closed":
                    continue
                await self.request(mid, row, "stop", {})
            current = self.impact(document)
            if current:
                self.stage(
                    mid,
                    "blocked_unconfirmed",
                    blockers=[
                        {
                            **impact_row(row),
                            "reason": "environment_maintenance_unconfirmed",
                        }
                        for row in current[:40]
                    ],
                )
                while not self.cancelled(mid):
                    await asyncio.sleep(0.1)
                return
            if self.cancelled(mid):
                return
            self.stage(mid, "installing", blockers=[])
            await self.service.verify_baseline(mid, installing=True)

            def before_write():
                document, _ = self.fence(mid)
                if document.get("cancelRequested"):
                    raise CoordinationError(
                        "environment_maintenance_cancelled_before_mutation"
                    )
                validate_roots(document["plan"]["writeRoots"], {"install_write"})
                if self.impact(document):
                    raise CoordinationError("environment_maintenance_unconfirmed")
                self.store.update(
                    mid, writeStarted=True, event={"kind": "write_started"}
                )

            await install_plan(
                document["plan"],
                private,
                Path(private["directory"]),
                config=self.runtime.config,
                before_write=before_write,
                on_process=lambda birth, proof: self.process(
                    mid, "install", birth, proof
                ),
                on_result=lambda result: self.result(mid, "install", result),
            )
            if self.cancelled(mid):
                return
            self.stage(mid, "verifying")
            verification = await verify_plan(
                private,
                Path(private["directory"]),
                config=self.runtime.config,
                on_process=lambda birth, proof: self.process(
                    mid, "verify", birth, proof
                ),
                before_probe=lambda: self.fence(mid),
                on_result=lambda result: self.result(mid, "verify", result),
            )
            verified = await self.refresh(mid, private)
            self.commit(mid, verified, verification)
            await self.restarts(mid)
        except asyncio.CancelledError:
            document, _ = self.store.read(mid)
            if document["writeStarted"]:
                self.damage(mid, "environment_maintenance_owner_unavailable")
            raise
        except (CoordinationError, ValueError, OSError) as error:
            document, _ = self.store.read(mid)
            reason = (
                str(error)
                if str(error).startswith("environment_maintenance_")
                and len(str(error)) < 128
                else "environment_maintenance_unconfirmed"
            )
            if document["writeStarted"]:
                self.damage(mid, reason)
            elif not self.cancelled(mid):
                self.stage(mid, "blocked_unconfirmed", blockers=[{"reason": reason}])
                while not self.cancelled(mid):
                    await asyncio.sleep(0.1)

    async def request(self, mid, row, kind, payload):
        action = digest([mid, row["reservation_id"], kind, payload])
        owner = json.loads(row["identity_json"])["identity"].get("ownerProcess")
        with self.coordinator.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO maintenance_actions VALUES(?,?,?,?,?,?,?,'pending',NULL)",
                (
                    action,
                    mid,
                    row["workspace_id"],
                    row["owner_id"],
                    row["owner_epoch"],
                    kind,
                    encoded(
                        {"reservationId": row["reservation_id"], "payload": payload}
                    ),
                ),
            )
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            with self.coordinator.transaction() as c:
                found = c.execute(
                    "SELECT * FROM maintenance_actions WHERE action_id=?", (action,)
                ).fetchone()
            if found["state"] == "done":
                return json.loads(found["result_json"])
            if found["state"] == "failed":
                raise CoordinationError(json.loads(found["result_json"])["reason"])
            if not same_process(owner) or asyncio.get_running_loop().time() >= deadline:
                raise CoordinationError("environment_maintenance_unconfirmed")
            document, _ = self.store.read(mid)
            if document.get("cancelRequested") and kind != "restart":
                raise CoordinationError(
                    "environment_maintenance_cancelled_before_mutation"
                )
            await asyncio.sleep(0.05)

    async def interrupt(self, mid, rows):
        for row in rows:
            if (
                row["kind"] in {"kernel", "job"}
                and self.coordinator.get(row["reservation_id"])["state"] != "closed"
            ):
                await self.request(mid, row, "interrupt", {})
        self.store.update(
            mid, interruptRequested=False, event={"kind": "interrupt_dispatched"}
        )

    async def refresh(self, mid, private):
        result = {}
        for row in self.verification_targets(private):
            result[row["workspace_id"]] = await self.request(mid, row, "verify", {})
        return result

    def plan_verification_targets(self, impact):
        rows = {self.runtime.identity["workspaceId"]: self.own_target()}
        for affected in impact:
            workspace = affected["workspace_id"]
            if affected["kind"] != "kernel" or workspace in rows:
                continue
            candidates = []
            for row in self.coordinator.workspace_reservations(workspace):
                identity = json.loads(row["identity_json"])["identity"]
                if (
                    row["kind"] == "control"
                    and row["state"] == "active"
                    and row["workspace_path"] == affected["workspace_path"]
                    and identity.get("role") == "notebook_owner"
                    and same_process(identity.get("ownerProcess"))
                ):
                    candidates.append(row)
            if len(candidates) != 1:
                raise CoordinationError("environment_maintenance_unconfirmed")
            rows[workspace] = candidates[0]
        return list(rows.values())

    def verification_targets(self, private):
        rows = private.get("verificationTargets", [])
        own = next(
            (
                r
                for r in rows
                if r["workspace_id"] == self.runtime.identity["workspaceId"]
            ),
            None,
        )
        if own != self.own_target() or any(
            not same_process(
                json.loads(r["identity_json"])["identity"].get("ownerProcess")
            )
            for r in rows
        ):
            raise CoordinationError("environment_maintenance_plan_stale")
        return rows

    def own_target(self):
        return {
            "reservation_id": "maintenance-owner-" + self.runtime.owner_id,
            "workspace_id": self.runtime.identity["workspaceId"],
            "owner_id": self.runtime.owner_id,
            "owner_epoch": self.runtime.epoch,
            "identity_json": encoded(
                {"identity": {"ownerProcess": self.service.guard.owner["process"]}}
            ),
        }

    def commit(self, mid, verified, verification):
        with self.coordinator.transaction() as c:
            row = self.store.row(c, mid)
            document, private = (
                json.loads(row["document_json"]),
                json.loads(row["private_json"]),
            )
            own = c.execute(
                "SELECT * FROM maintenance_fences WHERE maintenance_id=?", (mid,)
            ).fetchone()
            conflicts = list(
                self.coordinator._conflicts(c, document["plan"]["writeRoots"])
            )
            expected = {mid} | set(private["damageEpochs"])
            if {f["maintenance_id"] for f in conflicts} != expected or own[
                "state"
            ] != "verifying":
                raise CoordinationError("environment_maintenance_plan_stale")
            if any(
                f["state"] != "needs_repair"
                or f["fence_seq"] != private["damageEpochs"].get(f["maintenance_id"])
                for f in conflicts
                if f["maintenance_id"] != mid
            ):
                raise CoordinationError("environment_maintenance_plan_stale")
            if self.coordinator._impact(c, document["plan"]["writeRoots"], limit=1):
                raise CoordinationError("environment_maintenance_unconfirmed")
            restart_rows = []
            for affected in private["restartImpact"]:
                if affected["kind"] != "kernel":
                    continue
                identity = json.loads(affected["identity_json"])["identity"]
                environment = verified.get(affected["workspace_id"], {}).get(
                    identity["environmentId"]
                )
                if environment is None:
                    raise CoordinationError("environment_maintenance_unconfirmed")
                restart_rows.append(
                    {
                        "restartId": digest([mid, affected["reservation_id"]]),
                        "workspaceId": affected["workspace_id"],
                        "notebookId": identity.get("notebookId"),
                        "language": identity["language"],
                        "previousKernelInstanceId": affected["resource_id"],
                        "previousGeneration": identity["generation"],
                        "kernelInstanceId": None,
                        "generation": None,
                        "status": "restart_required",
                        "reason": "",
                        "environment": environment,
                        "target": affected,
                    }
                )
            private.update(
                verified=verified,
                restartRows=restart_rows,
                committedEpoch=own["fence_seq"],
            )
            c.execute(
                "UPDATE maintenance_records SET private_json=? WHERE maintenance_id=?",
                (encoded(private), mid),
            )
            c.executemany(
                "UPDATE maintenance_fences SET state='closed' WHERE maintenance_id=?",
                [(identity,) for identity in expected],
            )
            document.update(
                state="environment_committed",
                blockers=[],
                result={
                    "environmentVerified": True,
                    "environmentRevision": verified[
                        self.runtime.identity["workspaceId"]
                    ][document["environmentId"]]["revision"],
                    "verification": verification,
                    "restarts": self.public_restarts(restart_rows),
                },
            )
            self.store.write(
                c,
                document,
                event={"kind": "environment_committed", "scopeEpoch": own["fence_seq"]},
            )

    @staticmethod
    def public_restarts(rows):
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"environment", "target"}
            }
            for row in rows
        ]

    def check_restart_epoch(self, document, private):
        with self.coordinator.transaction() as c:
            roots = document["plan"]["writeRoots"]
            if list(self.coordinator._conflicts(c, roots)):
                raise CoordinationError("environment_maintenance_restart_stale")
            for fence in c.execute(
                "SELECT * FROM maintenance_fences WHERE fence_seq>?",
                (private["committedEpoch"],),
            ):
                from .environment_coordination import overlaps

                if any(
                    overlaps(a, b)
                    for a in roots
                    for b in json.loads(fence["roots_json"])
                ):
                    raise CoordinationError("environment_maintenance_restart_stale")

    async def check_restart(self, document, private, restart_id):
        if not (document.get("result") or {}).get("environmentVerified"):
            raise CoordinationError("environment_maintenance_invalid_state")
        self.check_restart_epoch(document, private)
        row = next(
            (r for r in private["restartRows"] if r["restartId"] == restart_id), None
        )
        if row is None:
            raise CoordinationError("environment_maintenance_not_found")
        await self.request(
            document["maintenanceId"],
            row["target"],
            "check_restart",
            {"restartId": restart_id, "attempt": self.coordinator_read_sequence()},
        )

    def coordinator_read_sequence(self):
        with self.coordinator.transaction() as c:
            return c.execute("SELECT sequence FROM metadata").fetchone()[0]

    async def retry_restart(self, mid, restart_id):
        await self.restarts(mid, only=restart_id)

    async def restarts(self, mid, *, only=None):
        document, private = self.store.read(mid)
        rows = private["restartRows"]
        for row in rows:
            if only and row["restartId"] != only:
                continue
            try:
                self.check_restart_epoch(document, private)
                result = await self.request(
                    mid,
                    row["target"],
                    "restart",
                    {
                        "restartId": row["restartId"],
                        "attempt": self.coordinator_read_sequence(),
                    },
                )
                outcome = result
            except (CoordinationError, ValueError, OSError) as error:
                outcome = {
                    "status": "restart_required",
                    "reason": str(error)
                    if str(error).startswith("environment_maintenance_")
                    else "environment_maintenance_restart_failed",
                }
            self.record_restart(mid, row["restartId"], outcome)
        with self.coordinator.transaction() as c:
            saved = self.store.row(c, mid)
            current, private = (
                json.loads(saved["document_json"]),
                json.loads(saved["private_json"]),
            )
            current["state"] = (
                "restart_required"
                if any(
                    r["status"] == "restart_required" for r in private["restartRows"]
                )
                else "completed"
            )
            self.store.write(c, current, event={"kind": "finished"})

    def record_restart(self, mid, restart_id, outcome):
        with self.coordinator.transaction() as c:
            saved = self.store.row(c, mid)
            document, private = (
                json.loads(saved["document_json"]),
                json.loads(saved["private_json"]),
            )
            current = next(
                r for r in private["restartRows"] if r["restartId"] == restart_id
            )
            # A concurrent stale refusal cannot undo a confirmed successful restart.
            if current["status"] != "ready" or outcome["status"] == "ready":
                current.update(outcome)
            c.execute(
                "UPDATE maintenance_records SET private_json=? WHERE maintenance_id=?",
                (encoded(private), mid),
            )
            document["result"]["restarts"] = self.public_restarts(
                private["restartRows"]
            )
            self.store.write(
                c,
                document,
                event={
                    "kind": "kernel_restart",
                    "restartId": restart_id,
                    "status": current["status"],
                },
            )

    async def mailbox(self):
        from .maintenance_mailbox import OwnerMaintenanceMailbox

        await OwnerMaintenanceMailbox(self.service).poll()
