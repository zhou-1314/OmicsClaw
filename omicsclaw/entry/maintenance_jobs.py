"""Route explicitly approved maintenance interrupts through the existing Job manager."""

import asyncio
import json

from .environment_coordination import CoordinationError, encoded


async def poll_job_interrupts(controller):
    guard = controller.environment_guard
    coordinator = guard.coordinator
    while True:
        with coordinator.transaction() as c:
            exists = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='maintenance_actions'"
            ).fetchone()
            rows = (
                c.execute(
                    "SELECT * FROM maintenance_actions WHERE workspace_id=? AND owner_id=? AND owner_epoch=? AND kind='interrupt' AND state='pending' LIMIT 8",
                    (
                        guard.workspace_id,
                        guard.owner["ownerId"],
                        guard.owner["ownerEpoch"],
                    ),
                ).fetchall()
                if exists
                else []
            )
        for action in rows:
            with coordinator.transaction() as c:
                data = json.loads(action["document_json"])
                record = c.execute(
                    "SELECT * FROM maintenance_records WHERE maintenance_id=?",
                    (action["maintenance_id"],),
                ).fetchone()
                if record is None:
                    continue
                document, private = (
                    json.loads(record["document_json"]),
                    json.loads(record["private_json"]),
                )
                original = next(
                    (
                        r
                        for r in private["impact"]
                        if r["reservation_id"] == data["reservationId"]
                    ),
                    None,
                )
                if not c.execute(
                    "UPDATE maintenance_actions SET state='running' WHERE action_id=? AND state='pending'",
                    (action["action_id"],),
                ).rowcount:
                    continue
            try:
                if (
                    original is None
                    or original["kind"] != "job"
                    or (original["owner_id"], original["owner_epoch"])
                    != (guard.owner["ownerId"], guard.owner["ownerEpoch"])
                    or not document.get("interruptRequested")
                    or document["state"]
                    not in {"draining", "checkpoint_required", "blocked_unconfirmed"}
                ):
                    raise CoordinationError("environment_maintenance_invalid_state")
                identity = json.loads(original["identity_json"])["identity"]
                job = controller.jobs.store.get_job(identity["jobId"])
                if (
                    job is None
                    or job.target_workdir != identity["targetWorkdir"]
                    or job.inputs.get("_science") != identity["science"]
                ):
                    raise CoordinationError("environment_maintenance_unconfirmed")
                result = {"status": await controller.jobs.cancel_job(job.id)}
                coordinator.reap_finished_jobs()
                state = "done"
            except (ValueError, OSError) as error:
                state, result = (
                    "failed",
                    {
                        "reason": str(error)
                        if str(error).startswith("environment_maintenance_")
                        else "environment_maintenance_unconfirmed"
                    },
                )
            with coordinator.transaction() as c:
                c.execute(
                    "UPDATE maintenance_actions SET state=?,result_json=? WHERE action_id=? AND state='running'",
                    (state, encoded(result), action["action_id"]),
                )
        await asyncio.sleep(0.1)
