"""Public restore admission and fixed readers share the owner's durable queue."""

import asyncio

import pytest

from omicsclaw.entry.notebook.checkpoint_store import reference
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_analysis_owner import reference as kernel_reference
from tests.entry.test_notebook_analysis_owner import settled
from tests.entry.test_notebook_checkpoint_owner import group, save_command, terminal
from tests.entry.test_notebook_r import bind, create
from tests.entry.test_notebook_r import command as user

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


async def restored(owner, oid):
    for _ in range(2000):
        op = owner.db.run(lambda c: owner.restores.get(c, "nb", oid))
        if op["state"] in {"restored", "failed", "unknown"}:
            return op
        if owner.durability_failed:
            raise AssertionError("durability failed")
        await asyncio.sleep(0.02)
    raise AssertionError(op)


async def source(owner, before_release=None):
    kernel = await group(owner)
    receipt = await owner.analysis_command(
        {"notebookId": "nb", "command": save_command(owner, kernel)}
    )
    op = await terminal(owner, receipt["receipt"]["result"]["operationId"])
    assert op["state"] == "ready", op
    checkpoint = owner.db.run(
        lambda c: owner.checkpoints.store.get(c, "nb", "checkpoint", op["checkpointId"])
    )
    if before_release:
        await before_release(owner, kernel, checkpoint)
    await owner.release(
        {
            "notebookId": "nb",
            "commandId": "release-source",
            "language": "python",
            "kernelInstanceId": kernel["kernelInstanceId"],
            "generation": kernel["generation"],
        }
    )
    env = owner.registry.get(kernel["environmentId"])
    target = (await bind(owner, "python", env, "restore", takeControl=True))["kernel"]
    return checkpoint, target


def restore_command(owner, checkpoint, target):
    return {
        "notebookId": "nb",
        "command": {
            "schemaVersion": 1,
            "commandId": "restore",
            "action": "checkpoint.restore",
            "arguments": {
                "checkpoint": reference(checkpoint, "checkpoint"),
                "target": kernel_reference(target),
            },
            "expected": {
                "analysisRevision": owner.analysis.store.state_response(
                    {"notebookId": "nb"}
                )["analysisRevision"],
                "kernels": [kernel_reference(target)],
                "objects": [],
                "selections": [],
            },
        },
    }


def test_real_table_group_restores_to_new_kernel_and_receipt_is_stable(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            checkpoint, target = await source(owner)
            command = restore_command(owner, checkpoint, target)
            accepted = await owner.analysis_command(command)
            assert accepted["receipt"]["result"]["entityKind"] == "checkpointRestore"
            oid = accepted["receipt"]["result"]["operationId"]
            op = await restored(owner, oid)
            assert op["state"] == "restored", op
            assert not op["gateActive"] and op["compatibility"]["status"] == "verified"
            assert (await owner.analysis_command(command))["receipt"] == accepted[
                "receipt"
            ]
            obj = owner.db.run(
                lambda c: owner.analysis.store.get(
                    c, "nb", "object", op["resultObjectRefs"][0]["objectId"]
                )
            )
            assert obj["kernel"] == kernel_reference(target)
            assert obj["shape"] == [2, 1]
            restored_data = user(target, "read-restored", "print(data.to_json())")
            restored_data["version"] = 3
            restored_data["preconditions"] = {
                "analysis": owner.db.run(
                    lambda c: owner.analysis.store.context(c, "nb")
                )["guards"]["python"]
            }
            result = await owner.admit({"command": restored_data})
            output = await settled(owner, result["execution_id"])
            assert output["status"] == "completed", output
            assert output["output"]["stdout"] == '{"count":{"a":1,"b":2}}\n'
            assert owner.db.run(lambda c: owner.restores.overview(c, "nb"))[
                "languages"
            ]["python"]["canRun"]
        finally:
            await owner.close()

    asyncio.run(run())


def test_corrupt_source_consumes_target_and_explicit_release_clears_only_its_gate(
    tmp_path,
):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            checkpoint, target = await source(owner)
            details = owner.db.run(
                lambda c: owner.checkpoints.store.details(c, checkpoint["operationId"])
            )
            path = (
                owner.checkpoints.directory
                / checkpoint["operationId"]
                / "data"
                / details["files"][0]["file"]
            )
            path.write_bytes(b"corrupt owned fixture")
            command = restore_command(owner, checkpoint, target)
            accepted = await owner.analysis_command(command)
            oid = accepted["receipt"]["result"]["operationId"]
            op = await restored(owner, oid)
            assert op["state"] == "failed" and op["gateActive"] and op["stopConfirmed"]
            assert (await owner.analysis_command(command))["receipt"] == accepted[
                "receipt"
            ]
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_checkpoint_holds WHERE kind='restore'"
                    ).fetchone()[0]
                )
                == 1
            )
            with pytest.raises(NotebookError, match="checkpoint_blocked"):
                await owner.admit(
                    {"command": user(target, "blocked", "must_not_run=True")}
                )
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release-failed",
                    "language": "python",
                    "kernelInstanceId": target["kernelInstanceId"],
                    "generation": target["generation"],
                }
            )
            op = owner.db.run(lambda c: owner.restores.get(c, "nb", oid))
            assert (
                op["state"] == "failed"
                and op["targetReleased"]
                and not op["gateActive"]
            )
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_checkpoint_holds WHERE kind='restore'"
                    ).fetchone()[0]
                )
                == 0
            )
            assert (
                owner.db.run(
                    lambda c: owner.checkpoints.store.get(
                        c, "nb", "checkpoint", checkpoint["checkpointId"]
                    )
                )["state"]
                == "ready"
            )
        finally:
            await owner.close()

    asyncio.run(run())


def test_unknown_loader_is_not_replayed_and_explicit_release_stops_target(tmp_path):
    import json

    from omicsclaw.entry.notebook.security import same_process
    from tests.entry.test_notebook_checkpoint_owner import wait_until

    async def run():
        owner = await create(tmp_path, real=True)
        marker, gate = tmp_path / "loader-started", tmp_path / "loader-gate"
        try:
            checkpoint, target = await source(owner)
            owner.checkpoints.limits["saveTimeoutSeconds"] = 0.3
            original = owner.restores.code

            def held(op, request, directory, *, commit=False):
                code = original(op, request, directory, commit=commit)
                if not commit:
                    return (
                        f"import signal,time,pathlib\nsignal.signal(signal.SIGINT,signal.SIG_IGN)\npathlib.Path({str(marker)!r}).touch()\nwhile not pathlib.Path({str(gate)!r}).exists(): time.sleep(.02)\n"
                        + code
                    )
                return code

            owner.restores.code = held
            params = restore_command(owner, checkpoint, target)
            accepted = await owner.analysis_command(params)
            oid = accepted["receipt"]["result"]["operationId"]
            read = lambda: owner.db.run(lambda c: owner.restores.get(c, "nb", oid))
            await wait_until(marker.exists)
            await wait_until(lambda: read()["state"] == "stopping_unconfirmed")
            assert not read()["stopConfirmed"]
            assert (await owner.analysis_command(params))["receipt"] == accepted[
                "receipt"
            ]
            with pytest.raises(NotebookError, match="checkpoint_blocked"):
                await owner.cancel(
                    {
                        "notebookId": "nb",
                        "executionId": read()["executionId"],
                        "commandId": "ordinary-cancel",
                        "kernelInstanceId": target["kernelInstanceId"],
                        "generation": target["generation"],
                    }
                )
            evidence = owner.db.run(
                lambda c: json.loads(
                    c.execute(
                        "SELECT process_json FROM notebook_runtime_kernels WHERE instance_id=?",
                        (target["kernelInstanceId"],),
                    ).fetchone()[0]
                )
            )
            assert same_process(evidence)
            async with asyncio.timeout(15):
                await owner.release(
                    {
                        "notebookId": "nb",
                        "commandId": "release-unknown",
                        "language": "python",
                        "kernelInstanceId": target["kernelInstanceId"],
                        "generation": target["generation"],
                    }
                )
            assert not same_process(evidence)
            assert read()["targetReleased"] and not read()["gateActive"]
            assert not gate.exists()
        finally:
            gate.touch()
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("language", ["python", "r"])
def test_real_scientific_group_restores_all_objects_and_original_dataset_refs(
    tmp_path, language
):
    import os

    from tests.entry.test_notebook_analysis_owner import registration
    from tests.entry.test_notebook_r import environment

    library = os.environ.get("OMICSCLAW_TEST_R_LIBRARY")
    if language == "r" and not library:
        pytest.skip("explicit R IRkernel library required")

    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(
                owner,
                language,
                real=True,
                libraries=[library] if language == "r" else None,
            )
            kernel = (await bind(owner, language, env))["kernel"]
            if language == "python":
                code = "import anndata as ad,pandas as pd,numpy as np\nfrom scipy.sparse import csr_matrix\ndata=ad.AnnData(csr_matrix([[1.,2.],[3.,4.]]),obs=pd.DataFrame({'cluster':['a','b']},index=['c1','c2']),var=pd.DataFrame(index=['g1','g2']))\ndata.layers['counts']=csr_matrix([[10,20],[30,40]])\ndata.obsm['X_umap']=np.array([[.25,.75],[.5,1.]])\nresult=pd.DataFrame({'score':[1.25,2.5]},index=['g1','g2'])"
                check = "assert data.obs_names.tolist()==['c1','c2']\nassert data.var_names.tolist()==['g1','g2']\nassert data.obs.cluster.tolist()==['a','b']\nassert data.layers['counts'].toarray().tolist()==[[10,20],[30,40]]\nassert data.X.toarray().tolist()==[[1.,2.],[3.,4.]]\nassert data.obsm['X_umap'].tolist()==[[.25,.75],[.5,1.]]\nassert result.score.tolist()==[1.25,2.5]\nprint('restored-whole-group')"
                adapters = ["anndata", "python_table"]
            else:
                code = "data<-DESeq2::DESeqDataSetFromMatrix(matrix(1:12,3,4,dimnames=list(c('g1','g2','g3'),c('s1','s2','s3','s4'))),data.frame(condition=factor(c('a','a','b','b')),row.names=c('s1','s2','s3','s4')),~condition);result<-data.frame(score=c(1.25,2.5,3.75),row.names=c('g1','g2','g3'))"
                check = "stopifnot(identical(unname(SummarizedExperiment::assay(data,'counts')),matrix(1:12,3,4)),identical(rownames(data),c('g1','g2','g3')),identical(colnames(data),c('s1','s2','s3','s4')),identical(as.character(SummarizedExperiment::colData(data)$condition),c('a','a','b','b')),identical(as.character(DESeq2::design(data)),c('~','condition')),identical(result$score,c(1.25,2.5,3.75)));cat('restored-whole-group\\n')"
                adapters = ["r_rds", "r_rds"]
            execution = await owner.admit(
                {"command": user(kernel, "create-group", code)}
            )
            assert (await settled(owner, execution["execution_id"]))[
                "status"
            ] == "completed"
            originals = []
            for index, (symbol, adapter) in enumerate(
                zip(["data", "result"], adapters, strict=True)
            ):
                cmd = registration(kernel, "register-" + symbol)
                cmd["arguments"].update(
                    symbol=symbol,
                    adapter=adapter,
                    rowIdentity={"kind": "index"},
                    columnIdentity={"kind": "index"},
                    declaredMatrix={
                        "assay": "counts" if language == "r" and index == 0 else None,
                        "layer": "counts"
                        if language == "python" and index == 0
                        else None,
                        "semantics": "raw_counts" if index == 0 else "unknown",
                    },
                )
                cmd["expected"]["analysisRevision"] = (
                    owner.analysis.store.state_response({"notebookId": "nb"})[
                        "analysisRevision"
                    ]
                )
                accepted = await owner.analysis_command(
                    {"notebookId": "nb", "command": cmd}
                )
                assert (
                    await settled(owner, accepted["receipt"]["result"]["operationId"])
                )["status"] == "completed"
                originals.append(
                    owner.db.run(
                        lambda c, accepted=accepted: owner.analysis.store.get(
                            c, "nb", "object", accepted["receipt"]["result"]["entityId"]
                        )
                    )
                )
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            saved = await terminal(owner, accepted["receipt"]["result"]["operationId"])
            assert saved["state"] == "ready", saved
            checkpoint = owner.db.run(
                lambda c: owner.checkpoints.store.get(
                    c, "nb", "checkpoint", saved["checkpointId"]
                )
            )
            await owner.release(
                {
                    "notebookId": "nb",
                    "language": language,
                    "commandId": "release-science",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                }
            )
            target = (await bind(owner, language, env, "new", takeControl=True))[
                "kernel"
            ]
            params = restore_command(owner, checkpoint, target)
            accepted = await owner.analysis_command(params)
            op = await restored(owner, accepted["receipt"]["result"]["operationId"])
            assert op["state"] == "restored", op
            assert len(op["resultObjectRefs"]) == 2
            imported = [
                owner.db.run(
                    lambda c, ref=ref: owner.analysis.store.get(
                        c, "nb", "object", ref["objectId"]
                    )
                )
                for ref in op["resultObjectRefs"]
            ]
            assert {x["symbol"]: x["dataset"] for x in imported} == {
                x["symbol"]: x["dataset"] for x in originals
            }
            assert all(x["kernel"] == kernel_reference(target) for x in imported)
            assert not {x["objectId"] for x in originals} & {
                x["objectId"] for x in imported
            }
            cmd = user(target, "verify-restored", check)
            cmd.update(
                version=3,
                preconditions={
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guards"][language]
                },
            )
            execution = await owner.admit({"command": cmd})
            final = await settled(owner, execution["execution_id"])
            assert final["status"] == "completed", final
            assert final["output"]["stdout"] == "restored-whole-group\n"
        finally:
            await owner.close()

    asyncio.run(run())
