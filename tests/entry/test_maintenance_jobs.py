"""A fixed maintenance interrupt reaches the existing Job cancellation interface."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omicsclaw.entry.environment_coordination import encoded
from omicsclaw.entry.maintenance_jobs import poll_job_interrupts
from omicsclaw.entry.maintenance_worker import MaintenanceWorker
from tests.entry.test_environment_coordination import reservation
from tests.entry.test_maintenance_adapter import wheel
from tests.entry.test_maintenance_boundaries import fake_prepare
from tests.entry.test_maintenance_service import approve, plan
from tests.entry.test_notebook_environment_guard import fixture


@pytest.mark.parametrize(
    "approved,changed_job,expected",
    [(True, False, "done"), (False, False, "failed"), (True, True, "failed")],
)
def test_only_confirmed_fixed_job_is_sent_to_existing_manager(
    tmp_path, monkeypatch, approved, changed_job, expected
):
    monkeypatch.setattr("omicsclaw.entry.maintenance.prepare_plan", fake_prepare)

    async def hold(*args):
        await asyncio.Future()

    monkeypatch.setattr(MaintenanceWorker, "run", hold)

    async def run():
        coordinator, owners = await fixture(tmp_path)
        owner, env, _ = owners[0]
        poller = None
        try:
            claim = owner.environment_guard.owner
            record = reservation(
                "job",
                [tmp_path / "science"],
                kind="job",
                owner=claim["ownerId"],
                workspace=owner.identity["workspaceId"],
            )
            record["identity"] = {
                "jobId": "actual-job",
                "targetWorkdir": "/fixed/job",
                "science": {"payloadDigest": "fixed"},
                "ownerProcess": claim["process"],
            }
            coordinator.reserve(record)
            coordinator.update("job", claim, committed=True)
            initial = (await plan(owner, env, wheel(tmp_path, "0.0.2"), "0.0.2"))[
                "maintenance"
            ]
            await approve(owner, initial)
            service = owner.maintenance_service
            service.store.update(
                initial["maintenanceId"], state="draining", interruptRequested=approved
            )
            with coordinator.transaction() as c:
                c.execute(
                    "INSERT INTO maintenance_actions VALUES(?,?,?,?,?,?,?,'pending',NULL)",
                    (
                        "interrupt-job",
                        initial["maintenanceId"],
                        owner.identity["workspaceId"],
                        claim["ownerId"],
                        claim["ownerEpoch"],
                        "interrupt",
                        encoded({"reservationId": "job", "payload": {}}),
                    ),
                )
            job = SimpleNamespace(
                id="actual-job",
                target_workdir="/changed" if changed_job else "/fixed/job",
                inputs={"_science": {"payloadDigest": "fixed"}},
            )
            cancel = AsyncMock(return_value="cancelled")
            controller = SimpleNamespace(
                environment_guard=SimpleNamespace(
                    coordinator=coordinator,
                    workspace_id=owner.identity["workspaceId"],
                    owner=claim,
                ),
                jobs=SimpleNamespace(
                    store=SimpleNamespace(
                        get_job=lambda job_id: job if job_id == job.id else None
                    ),
                    cancel_job=cancel,
                ),
            )
            poller = asyncio.create_task(poll_job_interrupts(controller))
            async with asyncio.timeout(5):
                while True:
                    with coordinator.transaction() as c:
                        action = c.execute(
                            "SELECT state,result_json FROM maintenance_actions WHERE action_id='interrupt-job'"
                        ).fetchone()
                    if action["state"] in {"done", "failed"}:
                        break
                    await asyncio.sleep(0.01)
            assert action["state"] == expected
            if expected == "done":
                cancel.assert_awaited_once_with("actual-job")
                assert json.loads(action["result_json"]) == {"status": "cancelled"}
            else:
                cancel.assert_not_awaited()
            # Routing a cancellation does not itself prove scientific descendants exited.
            assert coordinator.get("job")["state"] == "active"
        finally:
            if poller:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
            for runtime, _, _ in owners:
                await runtime.close()

    asyncio.run(run())
