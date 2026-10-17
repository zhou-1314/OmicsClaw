"""Stopped save failures permit fixed inspection without draining blocked code."""

import asyncio

import pytest

from omicsclaw.entry.notebook.analysis_store import entity_ref
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_analysis_owner import settled
from tests.entry.test_notebook_checkpoint_owner import decision_command, group, terminal
from tests.entry.test_notebook_r import bind, create, environment
from tests.entry.test_notebook_r import command as user

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def inspection(owner, cid):
    obj = owner.db.run(lambda c: owner.analysis.store.active_objects(c, "nb")[0])
    ref = entity_ref(obj, "object")
    return {
        "schemaVersion": 1,
        "commandId": cid,
        "action": "object.inspect",
        "arguments": {"object": ref},
        "expected": {
            "analysisRevision": owner.analysis.store.state_response(
                {"notebookId": "nb"}
            )["analysisRevision"],
            "kernels": [obj["kernel"]],
            "objects": [ref],
            "selections": [],
        },
    }


@pytest.mark.parametrize("agent", [False, True])
def test_stopped_failure_inspection_passes_only_its_blocked_queue(tmp_path, agent):
    async def run():
        owner = await create(tmp_path, real=True)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            kernel = await group(owner)
            owner.checkpoints.limits["maxFileBytes"] = 64
            original = owner.before_dispatch

            async def held(nid, eid):
                source = (await owner.service.store.get_execution(eid)).provenance[
                    "code"
                ]
                if "notebook_stage('qc')" in source:
                    entered.set()
                    await release.wait()
                await original(nid, eid)

            owner.before_dispatch = held
            guard = owner.db.run(lambda c: owner.analysis.store.context(c, "nb"))[
                "guard"
            ]
            first = user(kernel, "stage", "data['count']+=1\nnotebook_stage('qc')")
            first.update(version=3, preconditions={"analysis": guard})
            started = await owner.admit({"command": first})
            await asyncio.wait_for(entered.wait(), 10)
            later = user(kernel, "later", "forbidden=True")
            later.update(version=3, preconditions={"analysis": guard})
            queued = await owner.admit({"command": later})
            with pytest.raises(NotebookError, match="^notebook_busy$"):
                await owner.analysis_command(
                    {"notebookId": "nb", "command": inspection(owner, "normal-queue")}
                )
            release.set()
            assert (await settled(owner, started["execution_id"]))[
                "status"
            ] == "completed"
            operations = owner.analysis.store.page(
                {"notebookId": "nb", "collection": "checkpointOperations"}
            )["items"]
            op = await terminal(owner, operations[0]["operationId"])
            assert op["state"] == "failed" and op["stopConfirmed"]
            failure = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "failure", op["failureRef"]["failureId"]
                )
            )
            assert (
                owner.views.execution("nb", queued["execution_id"])["status"]
                == "queued"
            )

            # Fault-inject uncertainty at the durable failure boundary, without
            # changing the real queued source or dispatcher's interpretation.
            owner.service._transaction(
                lambda c: owner.checkpoints.store.put(
                    c, "nb", "failure", {**failure, "stopConfirmed": False}
                )
            )
            with pytest.raises(NotebookError, match="^notebook_busy$"):
                await owner.analysis_command(
                    {
                        "notebookId": "nb",
                        "command": inspection(owner, "unconfirmed-stop"),
                    }
                )
            owner.service._transaction(
                lambda c: owner.checkpoints.store.put(c, "nb", "failure", failure)
            )

            for status in ("running", "outcome_unknown"):
                owner.db.run(
                    lambda c, status=status: c.execute(
                        "UPDATE notebook_executions SET status=? WHERE execution_id=?",
                        (status, queued["execution_id"]),
                    )
                )
                try:
                    with pytest.raises(NotebookError, match="^notebook_busy$"):
                        await owner.analysis_command(
                            {"notebookId": "nb", "command": inspection(owner, status)}
                        )
                finally:
                    owner.db.run(
                        lambda c: c.execute(
                            "UPDATE notebook_executions SET status='queued' WHERE execution_id=?",
                            (queued["execution_id"],),
                        )
                    )

            command = inspection(owner, "inspect-stopped-failure")
            if not agent:
                original_admit = owner._admit

                async def uncertain_before_claim(*args, **kwargs):
                    owner.service._transaction(
                        lambda c: owner.checkpoints.store.put(
                            c, "nb", "failure", {**failure, "stopConfirmed": False}
                        )
                    )
                    return await original_admit(*args, **kwargs)

                owner._admit = uncertain_before_claim
                try:
                    with pytest.raises(NotebookError, match="checkpoint_blocked"):
                        await owner.analysis_command(
                            {"notebookId": "nb", "command": command}
                        )
                    assert not owner.actions.receipt(
                        {"notebookId": "nb", "commandId": command["commandId"]}
                    )["found"]
                finally:
                    owner._admit = original_admit
                    owner.service._transaction(
                        lambda c: owner.checkpoints.store.put(
                            c, "nb", "failure", failure
                        )
                    )
            mutation = owner.db.run(
                lambda c: owner.checkpoints.store.mutation(c, "nb", "python")
            )
            if agent:
                # Establish an Agent-owned request without granting Resume through
                # the save failure. The metadata tool still checks this snapshot.
                owner.service._transaction(
                    lambda c: c.execute(
                        "UPDATE notebook_request_control SET control_mode='agent' WHERE notebook_id='nb'"
                    )
                )
                context = await owner.context(
                    {"sessionId": "chat", "requestId": "diagnose"}
                )
                params = {
                    "action": "object.inspect",
                    "arguments": command["arguments"],
                    "commandId": command["commandId"],
                    "sessionId": "chat",
                    "requestId": "diagnose",
                    "token": context["token"],
                    "toolPosition": 0,
                }
                assert (
                    await owner.check_analysis_request(
                        {
                            k: params[k]
                            for k in ("sessionId", "requestId", "token", "action")
                        }
                    )
                )["current"]
                accepted = await owner.analysis_agent(params)
            else:
                accepted = await owner.analysis_command(
                    {"notebookId": "nb", "command": command}
                )
            inspected = await settled(
                owner, accepted["receipt"]["result"]["operationId"]
            )
            assert inspected["status"] == "completed", inspected
            assert (
                owner.views.execution("nb", queued["execution_id"])["status"]
                == "queued"
            )
            assert (
                owner.db.run(
                    lambda c: owner.checkpoints.store.mutation(c, "nb", "python")
                )
                == mutation
            )
            assert (
                owner.db.run(lambda c: owner.analysis.store.active_objects(c, "nb")[0])[
                    "availability"
                ]
                == "verified"
            )
            if agent:
                with pytest.raises(NotebookError, match="request_stale"):
                    await owner.check_analysis_request(
                        {
                            k: params[k]
                            for k in ("sessionId", "requestId", "token", "action")
                        }
                    )
            await owner.analysis_command(
                {
                    "notebookId": "nb",
                    "command": decision_command(
                        owner, failure, "checkpoint.continue", "continue"
                    ),
                }
            )
            rejected = await settled(owner, queued["execution_id"])
            assert rejected["status"] == "failed"
            assert (
                rejected["output"]["error"]["evalue"]
                == "notebook_analysis_reference_stale"
            )
            fresh = user(
                kernel,
                "confirm",
                "assert 'forbidden' not in globals()\nassert data['count'].tolist()==[2,3]",
            )
            fresh.update(
                version=3,
                preconditions={
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guard"]
                },
            )
            confirmed = await owner.admit({"command": fresh})
            assert (await settled(owner, confirmed["execution_id"]))[
                "status"
            ] == "completed"
        finally:
            release.set()
            await owner.close()

    asyncio.run(run())


def test_diagnostic_gate_does_not_skip_other_dispatchable_language(tmp_path):
    async def run():
        owner = await create(tmp_path)
        try:
            py = (await bind(owner, "python", await environment(owner, "python")))[
                "kernel"
            ]
            rk = (await bind(owner, "r", await environment(owner, "r")))["kernel"]
            # Stop only this fixture's scheduler so both public admissions remain
            # queued. Eligibility still uses the production dispatch predicate.
            owner.scheduler.cancel()
            await asyncio.gather(owner.scheduler, return_exceptions=True)
            await owner.admit({"command": user(py, "py-queued", "answer=1")})
            with pytest.raises(NotebookError, match="^notebook_busy$"):
                owner.db.run(lambda c: owner.checkpoints.inspection_idle(c, "nb"))

            def failure(language, confirmed=True):
                owner.service._transaction(
                    lambda c: owner.checkpoints.store.put(
                        c,
                        "nb",
                        "failure",
                        {
                            "failureId": language,
                            "failureRevision": 1,
                            "source": {"language": language},
                            "blocking": True,
                            "stopConfirmed": confirmed,
                        },
                    )
                )

            failure("python")
            owner.db.run(lambda c: owner.checkpoints.inspection_idle(c, "nb"))
            await owner.admit({"command": user(rk, "r-queued", "answer<-1")})
            with pytest.raises(NotebookError, match="^notebook_busy$"):
                owner.db.run(lambda c: owner.checkpoints.inspection_idle(c, "nb"))
            failure("r")
            owner.db.run(lambda c: owner.checkpoints.inspection_idle(c, "nb"))
            failure("r", confirmed=False)
            with pytest.raises(NotebookError, match="^notebook_busy$"):
                owner.db.run(lambda c: owner.checkpoints.inspection_idle(c, "nb"))
            failure("r")
            owner.service._transaction(
                lambda c: owner.checkpoints.store.put(
                    c,
                    "nb",
                    "operation",
                    {
                        "operationId": "saving-r",
                        "statusSeq": 1,
                        "source": {"language": "r"},
                        "state": "saving",
                    },
                )
            )
            with pytest.raises(NotebookError, match="^notebook_busy$"):
                owner.db.run(lambda c: owner.checkpoints.inspection_idle(c, "nb"))
        finally:
            await owner.close()

    asyncio.run(run())
