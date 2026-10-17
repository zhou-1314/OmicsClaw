"""Registered groups save through the real owner and its existing kernel queue."""

import asyncio

import pytest

from omicsclaw.entry.notebook.analysis_store import kernel_ref
from tests.entry.test_notebook_analysis_owner import registration, settled
from tests.entry.test_notebook_r import bind, create, environment
from tests.entry.test_notebook_r import command as user

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


async def group(owner):
    env = await environment(owner, "python", real=True)
    kernel = (await bind(owner, "python", env))["kernel"]
    result = await owner.admit(
        {
            "command": user(
                kernel,
                "make",
                "import pandas as pd\ndata=pd.DataFrame({'count':[1,2]},index=['a','b'])",
            )
        }
    )
    assert (await settled(owner, result["execution_id"]))["status"] == "completed"
    command = registration(kernel)
    command["arguments"].update(
        adapter="python_table",
        rowIdentity={"kind": "index"},
        columnIdentity={"kind": "index"},
        declaredMatrix={"assay": None, "layer": None, "semantics": "unknown"},
    )
    command["expected"]["analysisRevision"] = owner.analysis.store.state_response(
        {"notebookId": "nb"}
    )["analysisRevision"]
    result = await owner.analysis_command({"notebookId": "nb", "command": command})
    assert (await settled(owner, result["receipt"]["result"]["operationId"]))[
        "status"
    ] == "completed"
    return kernel


async def terminal(owner, oid):
    for _ in range(1000):
        op = owner.db.run(
            lambda c: owner.checkpoints.store.get(c, "nb", "operation", oid)
        )
        if op["state"] in {"ready", "failed", "unknown"}:
            return op
        if owner.durability_failed:
            raise AssertionError("owner durability failed")
        await asyncio.sleep(0.02)
    raise AssertionError(op)


def test_real_python_group_is_validated_and_atomically_published(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            kernel = await group(owner)
            source = owner.db.run(
                lambda c: kernel_ref(
                    c.execute(
                        "SELECT * FROM notebook_runtime_kernels WHERE instance_id=?",
                        (kernel["kernelInstanceId"],),
                    ).fetchone()
                )
            )
            cid, oid = owner.service._transaction(
                lambda c: owner.checkpoints.store.intent(c, "nb", source)
            )
            owner.wake()
            op = await terminal(owner, oid)
            assert op["state"] == "ready", (
                op,
                owner.db.run(lambda c: owner.checkpoints.store.details(c, oid)),
            )
            checkpoint = owner.db.run(
                lambda c: owner.checkpoints.store.get(c, "nb", "checkpoint", cid)
            )
            assert checkpoint["totalBytes"] > 0 and checkpoint["manifestSha256"]
            import pandas as pd

            evidence = owner.db.run(lambda c: owner.checkpoints.store.details(c, oid))[
                "manifest"
            ]["runtimeEvidence"]
            assert evidence["language"] == "python"
            assert evidence["packages"]["pandas"] == {
                "version": pd.__version__,
                "path": pd.__file__,
            }
            assert owner.db.run(
                lambda c: owner.checkpoints.store.blocked(c, "nb", "python")
            ) == ([], [])
        finally:
            await owner.close()

    asyncio.run(run())


def save_command(owner, kernel, cid="save"):
    from omicsclaw.entry.notebook.analysis_store import entity_ref
    from tests.entry.test_notebook_analysis_owner import reference

    state = owner.analysis.store.state_response({"notebookId": "nb"})
    refs = owner.db.run(
        lambda c: [
            entity_ref(v, "object")
            for v in owner.analysis.store.active_objects(c, "nb")
            if v["kernel"]["language"] == kernel["language"]
        ]
    )
    source = reference(kernel)
    return {
        "schemaVersion": 1,
        "commandId": cid,
        "action": "checkpoint.save",
        "arguments": {"kernel": source, "objects": refs, "policyRevision": 1},
        "expected": {
            "analysisRevision": state["analysisRevision"],
            "kernels": [source],
            "objects": refs,
            "selections": [],
        },
    }


def test_public_save_receipt_pages_pin_and_failure_continue(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            kernel = await group(owner)
            params = {"notebookId": "nb", "command": save_command(owner, kernel)}
            result = await owner.analysis_command(params)
            assert await owner.analysis_command(params) == result
            op = await terminal(owner, result["receipt"]["result"]["operationId"])
            assert op["state"] == "ready", op
            state = owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "checkpointOverview"}}
            )
            assert state["item"]["continuationReady"]
            checkpoints = owner.analysis.store.page(
                {"notebookId": "nb", "collection": "checkpoints", "limit": 1}
            )
            cp = checkpoints["items"][0]
            assert checkpoints["parentRevision"] is None
            bindings = owner.analysis.store.page(
                {
                    "notebookId": "nb",
                    "collection": "checkpointBindings",
                    "parentId": cp["checkpointId"],
                    "parentRevision": 1,
                }
            )
            assert len(bindings["items"]) == 1 and bindings["parentRevision"] == 1
            assert owner.artifacts.get_artifact(
                bindings["items"][0]["artifact"]["artifactId"]
            )
            from omicsclaw.entry.notebook.checkpoint_store import reference

            pin = {
                "schemaVersion": 1,
                "commandId": "pin",
                "action": "checkpoint.pin",
                "arguments": {
                    "checkpoint": reference(cp, "checkpoint"),
                    "expectedPinRevision": 0,
                    "pinned": True,
                },
                "expected": {
                    "analysisRevision": state["analysisRevision"],
                    "kernels": [],
                    "objects": [],
                    "selections": [],
                },
            }
            await owner.analysis_command({"notebookId": "nb", "command": pin})
            holds = owner.analysis.store.page(
                {
                    "notebookId": "nb",
                    "collection": "checkpointHolds",
                    "parentId": cp["checkpointId"],
                    "parentRevision": 1,
                }
            )
            assert len(holds["items"]) == 1 and holds["items"][0]["kind"] == "pin"
            owner.checkpoints.limits["maxFileBytes"] = 64
            failed = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel, "bad")}
            )
            op = await terminal(owner, failed["receipt"]["result"]["operationId"])
            assert op["state"] == "failed" and op["stopConfirmed"], op
            failure = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {
                        "kind": "checkpointFailure",
                        "id": op["failureRef"]["failureId"],
                    },
                }
            )["item"]
            continuation = {
                "schemaVersion": 1,
                "commandId": "continue",
                "action": "checkpoint.continue",
                "arguments": {
                    "failure": reference(failure, "failure"),
                    "acknowledgement": "continue_without_this_checkpoint",
                },
                "expected": {
                    "analysisRevision": owner.analysis.store.state_response(
                        {"notebookId": "nb"}
                    )["analysisRevision"],
                    "kernels": [failure["source"]],
                    "objects": failure["objects"],
                    "selections": [],
                },
            }
            await owner.analysis_command({"notebookId": "nb", "command": continuation})
            assert owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "checkpointOverview"}}
            )["item"]["continuationReady"]
            # The failed bounded writer never replaced or reset the resident table.
            command = user(kernel, "check", "assert data['count'].tolist()==[1,2]")
            command.update(
                version=3,
                preconditions={
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guard"]
                },
            )
            accepted = await owner.admit({"command": command})
            assert (await settled(owner, accepted["execution_id"]))[
                "status"
            ] == "completed"
        finally:
            await owner.close()

    asyncio.run(run())


def test_automatic_stage_reinspects_reassigned_filtered_anndata(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, "python", real=True)
            kernel = (await bind(owner, "python", env))["kernel"]
            source = "import anndata as ad,numpy as np,pandas as pd\nfrom scipy.sparse import csr_matrix\ndata=ad.AnnData(csr_matrix(np.arange(8).reshape(4,2)),obs=pd.DataFrame({'sample':['s']*4,'barcode':['a','b','c','d']},index=['a','b','c','d']),var=pd.DataFrame(index=['g1','g2']))\ndata.layers['counts']=data.X.copy()\noriginal_id=id(data)"
            first = await owner.admit({"command": user(kernel, "data", source)})
            assert (await settled(owner, first["execution_id"]))[
                "status"
            ] == "completed"
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": registration(kernel)}
            )
            assert (await settled(owner, accepted["receipt"]["result"]["operationId"]))[
                "status"
            ] == "completed"
            command = user(
                kernel,
                "filter",
                "data=data[[True,False,True,False]].copy()\ndata.obs['cluster']=['a','b']\nassert id(data)!=original_id\nnotebook_stage('clustering', {'method':'fixed-test'})",
            )
            command.update(
                version=3,
                preconditions={
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guard"]
                },
            )
            accepted = await owner.admit({"command": command})
            final = await settled(owner, accepted["execution_id"])
            assert final["status"] == "completed", final
            stages = owner.analysis.store.page(
                {"notebookId": "nb", "collection": "checkpointOperations"}
            )["items"]
            assert len(stages) == 1
            op = await terminal(owner, stages[0]["operationId"])
            assert op["state"] == "ready", op
            details = owner.db.run(
                lambda c: owner.checkpoints.store.details(c, op["operationId"])
            )
            assert [v["count"] for v in details["manifest"]["bindings"][0]["axes"]] == [
                2,
                2,
            ]
            import anndata

            path = owner.artifacts.get_artifact(
                details["manifest"]["bindings"][0]["artifact"]["artifactId"]
            ).path
            restored = anndata.read_h5ad(path)
            assert restored.obs_names.tolist() == ["a", "c"] and restored.obs[
                "cluster"
            ].tolist() == ["a", "b"]
            assert details["manifest"]["metadata"] == {"method": "fixed-test"}
        finally:
            await owner.close()

    asyncio.run(run())


def test_real_r_group_public_save_and_automatic_stage(tmp_path):
    import os

    library = os.environ.get("OMICSCLAW_TEST_R_LIBRARY")
    if not library:
        pytest.skip("explicit R IRkernel library required")

    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, "r", real=True, libraries=[library])
            kernel = (await bind(owner, "r", env))["kernel"]
            result = await owner.admit(
                {
                    "command": user(
                        kernel,
                        "r-data",
                        "data<-data.frame(count=c(1L,2L),row.names=c('a','b'));"
                        "dds<-DESeq2::DESeqDataSetFromMatrix(matrix(1:12,3,4,dimnames=list(c('g1','g2','g3'),c('s1','s2','s3','s4'))),"
                        "data.frame(condition=factor(c('a','a','b','b')),row.names=c('s1','s2','s3','s4')),~condition)",
                    )
                }
            )
            assert (await settled(owner, result["execution_id"]))[
                "status"
            ] == "completed"
            command = registration(kernel)
            command["arguments"].update(
                adapter="r_rds",
                rowIdentity={"kind": "index"},
                columnIdentity={"kind": "index"},
                declaredMatrix={"assay": None, "layer": None, "semantics": "unknown"},
            )
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": command}
            )
            assert (await settled(owner, accepted["receipt"]["result"]["operationId"]))[
                "status"
            ] == "completed"
            command["commandId"] = "dds-registration"
            command["arguments"].update(
                symbol="dds",
                declaredMatrix={
                    "assay": "counts",
                    "layer": None,
                    "semantics": "raw_counts",
                },
            )
            command["expected"]["analysisRevision"] = (
                owner.analysis.store.state_response({"notebookId": "nb"})[
                    "analysisRevision"
                ]
            )
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": command}
            )
            assert (await settled(owner, accepted["receipt"]["result"]["operationId"]))[
                "status"
            ] == "completed"
            result = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            op = await terminal(owner, result["receipt"]["result"]["operationId"])
            assert op["state"] == "ready", op
            command = user(
                kernel,
                "r-stage",
                "data$count<-data$count+1L;notebook_stage('normalization',list(method='increment'))",
            )
            command.update(
                version=3,
                preconditions={
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guard"]
                },
            )
            result = await owner.admit({"command": command})
            final = await settled(owner, result["execution_id"])
            assert final["status"] == "completed", final
            operations = owner.analysis.store.page(
                {"notebookId": "nb", "collection": "checkpointOperations"}
            )["items"]
            automatic = next(v for v in operations if v["stage"])
            op = await terminal(owner, automatic["operationId"])
            assert op["state"] == "ready", op
            details = owner.db.run(
                lambda c: owner.checkpoints.store.details(c, op["operationId"])
            )
            assert details["files"][0]["format"] == "rds3"
            assert {item["type"] for item in details["files"]} == {
                "R.data.frame",
                "R.DESeqDataSet",
            }
            assert details["manifest"]["sourceExecutionId"] == result["execution_id"]
            assert details["manifest"]["runtimeEvidence"]["language"] == "r"
            assert details["manifest"]["runtimeEvidence"]["packages"]["DESeq2"][
                "version"
            ]
            assert details["manifest"]["runtimeEvidence"]["libraryPaths"][0] == library
        finally:
            await owner.close()

    asyncio.run(run())


async def wait_until(predicate, seconds=20):
    async with asyncio.timeout(seconds):
        while not (value := predicate()):
            await asyncio.sleep(0.02)
        return value


def decision_command(owner, failure, action, cid):
    from omicsclaw.entry.notebook.checkpoint_store import reference

    args = {"failure": reference(failure, "failure")}
    if action == "checkpoint.continue":
        args["acknowledgement"] = "continue_without_this_checkpoint"
    return {
        "schemaVersion": 1,
        "commandId": cid,
        "action": action,
        "arguments": args,
        "expected": {
            "analysisRevision": owner.analysis.store.state_response(
                {"notebookId": "nb"}
            )["analysisRevision"],
            "kernels": [failure["source"]],
            "objects": failure["objects"],
            "selections": [],
        },
    }


def test_actual_inspector_deadline_preserves_kernel_and_requires_confirmed_stop(
    tmp_path,
):
    from omicsclaw.entry.notebook.service import NotebookError

    async def run():
        owner = await create(tmp_path, real=True)
        gate, marker = tmp_path / "inspection-gate", tmp_path / "inspection-started"
        try:
            kernel = await group(owner)
            owner.checkpoints.limits["saveTimeoutSeconds"] = 0.3
            original = owner.checkpoints.source

            def held(phase, op, details, directory):
                source = original(phase, op, details, directory)
                if phase == "inspecting":
                    return (
                        f"import signal,time,pathlib\n__saved_handler=signal.signal(signal.SIGINT,signal.SIG_IGN)\npathlib.Path({str(marker)!r}).touch()\nwhile not pathlib.Path({str(gate)!r}).exists(): time.sleep(.02)\nsignal.signal(signal.SIGINT,__saved_handler)\n"
                        + source
                    )
                return source

            owner.checkpoints.source = held
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            oid = accepted["receipt"]["result"]["operationId"]
            read = lambda: owner.db.run(
                lambda c: owner.checkpoints.store.get(c, "nb", "operation", oid)
            )
            await wait_until(marker.exists)
            await wait_until(lambda: read()["state"] == "stopping_unconfirmed")
            op = read()
            assert not op["stopConfirmed"]
            with pytest.raises(NotebookError, match="checkpoint_blocked"):
                await owner.cancel(
                    {
                        "notebookId": "nb",
                        "executionId": op["executionId"],
                        "commandId": "cannot-cancel",
                        "kernelInstanceId": kernel["kernelInstanceId"],
                        "generation": kernel["generation"],
                    }
                )
            with pytest.raises(NotebookError, match="checkpoint_blocked"):
                await owner.release(
                    {
                        "notebookId": "nb",
                        "commandId": "cannot-release",
                        "kernelInstanceId": kernel["kernelInstanceId"],
                        "generation": kernel["generation"],
                    }
                )
            assert not owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "checkpointOverview"}}
            )["item"]["continuationReady"]
            gate.touch()
            op = await terminal(owner, oid)
            assert (
                op["state"] == "failed"
                and op["stopConfirmed"]
                and op["deadlineExceeded"]
            )
            failure = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "failure", op["failureRef"]["failureId"]
                )
            )
            await owner.analysis_command(
                {
                    "notebookId": "nb",
                    "command": decision_command(
                        owner, failure, "checkpoint.continue", "continue"
                    ),
                }
            )
            command = user(
                kernel, "same-object", "assert data['count'].tolist()==[1,2]"
            )
            # The timed-out inspection did not publish new references. Inspect is required.
            obj = owner.db.run(lambda c: owner.analysis.store.active_objects(c, "nb"))[
                0
            ]
            assert obj["kernel"]["kernelInstanceId"] == kernel["kernelInstanceId"]
            command.update(
                version=3,
                preconditions={
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guard"]
                },
            )
            result = await owner.admit({"command": command})
            assert (await settled(owner, result["execution_id"]))[
                "status"
            ] == "completed"
        finally:
            gate.touch()
            await owner.close()

    asyncio.run(run())


def test_stage_failure_blocks_preaccepted_queue_and_continue_can_resolve_it(tmp_path):
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
                if "notebook_stage" in source:
                    entered.set()
                    await release.wait()
                return await original(nid, eid)

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
            release.set()
            assert (await settled(owner, started["execution_id"]))[
                "status"
            ] == "completed"
            operations = owner.analysis.store.page(
                {"notebookId": "nb", "collection": "checkpointOperations"}
            )["items"]
            op = await terminal(owner, operations[0]["operationId"])
            assert op["state"] == "failed" and op["stopConfirmed"]
            assert (
                owner.db.run(
                    lambda c: owner.checkpoints.store.details(c, op["operationId"])
                )["sourceExecutionId"]
                == started["execution_id"]
            )
            assert (
                owner.views.execution("nb", queued["execution_id"])["status"]
                == "queued"
            )
            failure = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "failure", op["failureRef"]["failureId"]
                )
            )
            await owner.analysis_command(
                {
                    "notebookId": "nb",
                    "command": decision_command(
                        owner, failure, "checkpoint.continue", "continue"
                    ),
                }
            )
            assert (await settled(owner, queued["execution_id"]))["status"] == "failed"
            fresh = user(
                kernel,
                "read",
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
            executed = await owner.admit({"command": fresh})
            assert (await settled(owner, executed["execution_id"]))[
                "status"
            ] == "completed"
        finally:
            release.set()
            await owner.close()

    asyncio.run(run())


def test_publishing_reconciles_only_metadata_after_owner_restart(tmp_path):
    import sqlite3

    async def run():
        owner = await create(tmp_path, real=True)
        try:
            kernel = await group(owner)
            original = owner.artifacts.insert_artifact

            def crash(record):
                original(record)
                raise sqlite3.OperationalError("injected after artifact commit")

            owner.artifacts.insert_artifact = crash
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            oid = accepted["receipt"]["result"]["operationId"]
            await wait_until(lambda: owner.durability_failed)
            op = owner.db.run(
                lambda c: owner.checkpoints.store.get(c, "nb", "operation", oid)
            )
            assert op["state"] == "publishing"
            details = owner.db.run(lambda c: owner.checkpoints.store.details(c, oid))
            path = (
                owner.checkpoints.directory / oid / "data" / details["files"][0]["file"]
            )
            before = path.read_bytes(), path.stat().st_mtime_ns
            count = owner.db.run(
                lambda c: c.execute(
                    "SELECT count(*) FROM notebook_executions"
                ).fetchone()[0]
            )
        finally:
            await owner.close()
        recovered = await create(tmp_path, real=True)
        try:
            op = await terminal(recovered, oid)
            assert op["state"] == "ready"
            assert (path.read_bytes(), path.stat().st_mtime_ns) == before
            assert (
                recovered.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_executions"
                    ).fetchone()[0]
                )
                == count
            )
            assert (
                len(
                    recovered.analysis.store.page(
                        {
                            "notebookId": "nb",
                            "collection": "checkpointBindings",
                            "parentId": op["checkpointId"],
                            "parentRevision": 1,
                        }
                    )["items"]
                )
                == 1
            )
        finally:
            await recovered.close()

    asyncio.run(run())


def test_ready_group_gc_preserves_holds_and_unpublished_bytes(tmp_path):
    import copy
    import hashlib
    import shutil
    import time

    from omicsclaw.entry.notebook.checkpoint_store import digest
    from omicsclaw.entry.notebook.contracts import encoded
    from omicsclaw.entry.notebook.service import NotebookError

    async def run():
        owner = await create(tmp_path, real=True)
        try:
            kernel = await group(owner)
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            original_op = await terminal(
                owner, accepted["receipt"]["result"]["operationId"]
            )
            assert original_op["state"] == "ready"
            original = owner.db.run(
                lambda c: owner.checkpoints.store.details(c, original_op["operationId"])
            )
            pinned = original_op["checkpointId"]
            owner.db.run(
                lambda c: c.execute(
                    "INSERT INTO notebook_checkpoint_holds(checkpoint_id,hold_id,kind,reference_id,created_at) VALUES(?,?,?,?,?)",
                    (pinned, "pin-test", "pin", pinned, time.time()),
                )
            )
            published = [pinned]
            for index in range(7):
                cid, oid = owner.service._transaction(
                    lambda c: owner.checkpoints.store.intent(
                        c, "nb", original_op["source"]
                    )
                )
                details = copy.deepcopy(original)
                details.pop("manifest")
                destination = owner.checkpoints.directory / oid / "data"
                destination.mkdir(parents=True)
                for item in details["files"]:
                    item["artifactId"] = hashlib.sha256(
                        encoded([oid, item["logicalKey"]])
                    ).hexdigest()
                    shutil.copyfile(
                        owner.checkpoints.directory
                        / original_op["operationId"]
                        / "data"
                        / item["file"],
                        destination / item["file"],
                    )

                def ready_for_publication(
                    c, details=details, oid=oid, cid=cid, index=index
                ):
                    c.execute(
                        "UPDATE notebook_checkpoint_intents SET document=? WHERE operation_id=?",
                        (encoded(details).decode(), oid),
                    )
                    owner.checkpoints.store.transition(
                        c,
                        "nb",
                        oid,
                        "validating",
                        expected={"intent"},
                        bindingsDigest=digest(details["bindings"]),
                        stopConfirmed=True,
                    )
                    if index == 0:
                        c.execute(
                            "INSERT INTO notebook_checkpoint_holds(checkpoint_id,hold_id,kind,reference_id,created_at) VALUES(?,?,?,?,?)",
                            (
                                cid,
                                "job-intent-test",
                                "job_intent",
                                "fixed-future-job",
                                time.time(),
                            ),
                        )

                owner.service._transaction(ready_for_publication)
                await owner.checkpoints.publish("nb", oid)
                published.append(cid)
            visible = owner.analysis.store.page(
                {"notebookId": "nb", "collection": "checkpoints", "limit": 40}
            )["items"]
            ids = {value["checkpointId"] for value in visible}
            assert (
                pinned in ids
                and published[1] in ids
                and published[2] not in ids
                and len(ids) == 7
            )
            with pytest.raises(NotebookError, match="checkpoint_not_found"):
                owner.analysis.store.state_response(
                    {
                        "notebookId": "nb",
                        "subject": {"kind": "checkpoint", "id": published[2]},
                    }
                )
            orphan = owner.checkpoints.directory / "unknown-staging"
            orphan.mkdir()
            (orphan / "bytes").write_bytes(b"held-without-row")
            owner.db.run(
                lambda c: c.execute(
                    "DELETE FROM notebook_checkpoint_holds WHERE hold_id='job-intent-test'"
                )
            )
            owner.checkpoints.collect("nb")
            assert (orphan / "bytes").read_bytes() == b"held-without-row"
            assert owner.analysis.files.disk_usage() >= len(b"held-without-row")
        finally:
            await owner.close()

    asyncio.run(run())


def test_retry_chain_and_lost_receipt_keep_original_operation(tmp_path):
    from omicsclaw.entry.notebook.service import NotebookError

    async def run():
        owner = await create(tmp_path, real=True)
        try:
            kernel = await group(owner)
            owner.checkpoints.limits["maxFileBytes"] = 64
            initial = {
                "notebookId": "nb",
                "command": save_command(owner, kernel, "first"),
            }
            receipt = await owner.analysis_command(initial)
            first = await terminal(owner, receipt["receipt"]["result"]["operationId"])
            assert first["state"] == "failed"
            failure1 = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "failure", first["failureRef"]["failureId"]
                )
            )
            from omicsclaw.entry.notebook.analysis_store import entity_ref

            obj = owner.db.run(
                lambda c: owner.analysis.store.active_objects(c, "nb")[0]
            )
            inspected = await owner.analysis_command(
                {
                    "notebookId": "nb",
                    "command": {
                        "schemaVersion": 1,
                        "commandId": "inspect-after-failure",
                        "action": "object.inspect",
                        "arguments": {"object": entity_ref(obj, "object")},
                        "expected": {
                            "analysisRevision": owner.analysis.store.state_response(
                                {"notebookId": "nb"}
                            )["analysisRevision"],
                            "kernels": [obj["kernel"]],
                            "objects": [entity_ref(obj, "object")],
                            "selections": [],
                        },
                    },
                }
            )
            assert (
                await settled(owner, inspected["receipt"]["result"]["operationId"])
            )["status"] == "completed"
            # Unknown transport receipt recovery uses the immutable command, despite new revisions.
            assert (await owner.analysis_command(initial))["receipt"] == receipt[
                "receipt"
            ]
            assert (
                owner.actions.receipt({"notebookId": "nb", "commandId": "first"})[
                    "receipt"
                ]
                == receipt["receipt"]
            )
            retry = await owner.analysis_command(
                {
                    "notebookId": "nb",
                    "command": decision_command(
                        owner, failure1, "checkpoint.retry", "retry"
                    ),
                }
            )
            second = await terminal(owner, retry["receipt"]["result"]["operationId"])
            failure2 = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "failure", second["failureRef"]["failureId"]
                )
            )
            assert (
                failure2["rootFailureId"] == failure1["failureId"]
                and failure2["stopConfirmed"]
            )
            with pytest.raises(NotebookError, match="checkpoint_reference_stale"):
                await owner.analysis_command(
                    {
                        "notebookId": "nb",
                        "command": decision_command(
                            owner, failure1, "checkpoint.continue", "stale-continue"
                        ),
                    }
                )
            owner.checkpoints.limits["maxFileBytes"] = 1024 * 1024
            fixed = await owner.analysis_command(
                {
                    "notebookId": "nb",
                    "command": decision_command(
                        owner, failure2, "checkpoint.retry", "retry-fixed"
                    ),
                }
            )
            final = await terminal(owner, fixed["receipt"]["result"]["operationId"])
            assert final["state"] == "ready"
            current = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "failure", failure2["failureId"]
                )
            )
            assert (
                not current["blocking"]
                and current["resolvedByCheckpoint"]["checkpointId"]
                == final["checkpointId"]
            )
            assert (
                len(
                    owner.analysis.store.page(
                        {"notebookId": "nb", "collection": "checkpointFailures"}
                    )["items"]
                )
                == 2
            )
            assert owner.analysis.files.disk_usage() < owner.limits["diskBytes"]
            assert not (owner.checkpoints.directory / first["operationId"]).exists()
        finally:
            await owner.close()

    asyncio.run(run())


def test_agent_save_uses_permission_and_recovers_only_original_receipt(tmp_path):
    import json

    from omicsclaw.engine import AgentEngine, EngineConfig
    from omicsclaw.entry.notebook.binding import SSHNotebookBinding
    from omicsclaw.entry.notebook.service import NotebookError
    from omicsclaw.permission import PermissionGate, gate_tools
    from omicsclaw.schema import Message, Role, ToolCall
    from omicsclaw.tools.builtin.notebook_analysis import NotebookAnalysisTool
    from omicsclaw.tools.builtin.python_kernel import PythonKernelTool
    from omicsclaw.tools.context import use_tool_context
    from omicsclaw.tools.registry import ToolRegistry
    from tests.engine.test_augmentor import Provider
    from tests.entry.test_notebook_analysis_agent import LostAck

    class Client(LostAck):
        async def call(self, method, params, **kwargs):
            if method == "analysis_state":
                return self.runtime.analysis.store.state_response(params)
            return await super().call(method, params, **kwargs)

    async def run():
        owner = await create(tmp_path, real=True)
        try:
            kernel = await group(owner)
            # The fixture's initial user edits have been explicitly handed back.
            owner.service._transaction(
                lambda c: c.execute(
                    "UPDATE notebook_request_control SET control_mode='agent' WHERE notebook_id='nb'"
                )
            )
            client = Client(owner, action="checkpoint.save")
            binding = SSHNotebookBinding(client)
            tools = ToolRegistry(
                gate_tools(
                    (NotebookAnalysisTool(binding), PythonKernelTool(runner=binding)),
                    PermissionGate(),
                ),
                before_execute=binding.before_tool,
            )
            provider = Provider(
                Message(
                    role=Role.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="save",
                            name="notebook_analysis",
                            arguments=json.dumps(
                                {
                                    "action": "checkpoint.save",
                                    "arguments": save_command(owner, kernel)[
                                        "arguments"
                                    ],
                                }
                            ),
                        ),
                        ToolCall(
                            id="stale",
                            name="python",
                            arguments=json.dumps({"code": "must_not_execute=True"}),
                        ),
                    ),
                ),
                Message(role=Role.ASSISTANT, content="Saved the registered group."),
            )
            approvals = []

            async def approve(request):
                approvals.append(request)
                return True

            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                result = await asyncio.wait_for(
                    AgentEngine(provider, tools, EngineConfig()).run(
                        (
                            Message(
                                role=Role.USER, content="Save the registered analysis."
                            ),
                        ),
                        augmentor=binding.notebook_augmentor("chat"),
                    ),
                    40,
                )
            assert len(approvals) == 1
            assert client.accepted and client.observations[0]["found"]
            operation = await terminal(
                owner, client.observations[0]["receipt"]["result"]["operationId"]
            )
            assert operation["state"] == "ready"
            assert any(
                message.is_error and "notebook_request_stale" in message.content
                for message in result.messages
            )
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_checkpoint_intents"
                    ).fetchone()[0]
                )
                == 1
            )
            assert not any(
                '"token"' in message.content
                for sent in provider.seen
                for message in sent
            )
            fresh = await owner.context({"sessionId": "chat", "requestId": "fresh"})
            invocation = {
                **client.accepted,
                "token": fresh["token"],
                "requestId": "fresh",
                "commandId": "bad",
                "arguments": {},
                "action": "checkpoint.save",
            }
            with pytest.raises(
                NotebookError, match="notebook_checkpoint_invalid_command"
            ):
                await owner.analysis_agent(invocation)
            with pytest.raises(
                NotebookError, match="notebook_analysis_invalid_command"
            ):
                await owner.analysis_agent(
                    {**invocation, "action": "checkpoint.continue"}
                )
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_checkpoint_intents"
                    ).fetchone()[0]
                )
                == 1
            )
        finally:
            await owner.close()

    asyncio.run(run())
