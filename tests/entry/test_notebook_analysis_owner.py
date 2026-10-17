"""The actual owner queue publishes inspected objects and fences later code."""

import asyncio

import pytest

from omicsclaw.entry.notebook.analysis_store import entity_ref
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_r import bind, command, create, environment

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def reference(kernel):
    return {
        key: kernel[key]
        for key in (
            "language",
            "kernelInstanceId",
            "generation",
            "environmentId",
            "environmentRevision",
        )
    }


def registration(kernel, cid="register"):
    return {
        "schemaVersion": 1,
        "commandId": cid,
        "action": "object.register",
        "expected": {
            "analysisRevision": 0,
            "kernels": [reference(kernel)],
            "objects": [],
            "selections": [],
        },
        "arguments": {
            "kernel": reference(kernel),
            "symbol": "data",
            "adapter": "anndata",
            "sourceRefs": [],
            "declaredMetadata": {
                "organism": None,
                "sampleColumn": "sample",
                "conditionColumn": None,
                "donorColumn": None,
            },
            "rowIdentity": {"kind": "columns", "names": ["sample", "barcode"]},
            "columnIdentity": {"kind": "index"},
            "tableAxis": "row",
            "declaredMatrix": {
                "assay": None,
                "layer": "counts",
                "semantics": "raw_counts",
            },
        },
    }


async def settled(owner, eid):
    async with asyncio.timeout(30):
        while True:
            record = owner.views.execution("nb", eid)
            if (
                record["status"] not in {"queued", "running"}
                and record["durability"] != "pending"
                and eid not in owner.service.tasks
            ):
                return record
            await asyncio.sleep(0.02)


def test_real_owner_inspection_selection_invalidation_and_explicit_remap(tmp_path):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, "python", real=True)
            kernel = (await bind(owner, "python", env))["kernel"]
            source = """import anndata as ad, pandas as pd, numpy as np
from scipy.sparse import csr_matrix
data = ad.AnnData(csr_matrix(np.arange(12).reshape(6,2)), obs=pd.DataFrame({'sample':['s']*6, 'barcode':[f'cell{i}' for i in range(6)]},index=[f'cell{i}' for i in range(6)]),var=pd.DataFrame(index=['g1','g2']))
data.layers['counts']=data.X.copy()
"""
            created = await owner.admit({"command": command(kernel, "create", source)})
            assert (await settled(owner, created["execution_id"]))[
                "status"
            ] == "completed"
            params = {"notebookId": "nb", "command": registration(kernel)}
            accepted = await owner.analysis_command(params)
            assert await owner.analysis_command(params) == accepted
            result = await settled(owner, accepted["receipt"]["result"]["operationId"])
            assert result["status"] == "completed", result
            oid = accepted["receipt"]["result"]["entityId"]
            state = owner.analysis.store.state_response({"notebookId": "nb"})
            obj = owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "object", "id": oid}}
            )["item"]
            assert obj["availability"] == "verified" and obj["shape"] == [6, 2]
            assert obj["matrix"]["semanticsEvidence"] == "declared"
            rows = owner.analysis.store.page(
                {
                    "notebookId": "nb",
                    "collection": "viewRows",
                    "parentId": obj["defaultView"]["viewId"],
                    "viewRevision": obj["defaultView"]["viewRevision"],
                }
            )
            assert [row["id"] for row in rows["items"]] == [
                ["s", f"cell{i}"] for i in range(6)
            ]
            select = {
                "schemaVersion": 1,
                "commandId": "select",
                "action": "selection.create",
                "expected": {
                    "analysisRevision": state["analysisRevision"],
                    "kernels": [reference(kernel)],
                    "objects": [entity_ref(obj, "object")],
                    "selections": [],
                },
                "arguments": {
                    "view": obj["defaultView"],
                    "members": {
                        "kind": "ids",
                        "values": [["s", "cell1"], ["s", "cell4"]],
                    },
                },
            }
            picked = await owner.analysis_command(
                {"notebookId": "nb", "command": select}
            )
            sid = picked["receipt"]["result"]["entityId"]
            selected = owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "selection", "id": sid}}
            )["item"]
            assert selected["count"] == 2 and selected["valid"]
            context = owner.db.run(lambda c: owner.analysis.store.context(c, "nb"))
            run = {
                **command(kernel, "reorder", "data = data[::-1].copy()"),
                "version": 3,
                "preconditions": {"analysis": context["guard"]},
            }
            admitted = await owner.admit({"command": run})
            assert (await settled(owner, admitted["execution_id"]))[
                "status"
            ] == "completed"
            with pytest.raises(NotebookError, match="reference_stale"):
                await owner.admit(
                    {"command": {**run, "commandId": "stale", "cellId": "stale"}}
                )
            old = owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "selection", "id": sid}}
            )["item"]
            assert old["valid"] is False
            obj = owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "object", "id": oid}}
            )["item"]
            state = owner.analysis.store.state_response({"notebookId": "nb"})
            inspect = {
                "schemaVersion": 1,
                "commandId": "inspect",
                "action": "object.inspect",
                "expected": {
                    "analysisRevision": state["analysisRevision"],
                    "kernels": [reference(kernel)],
                    "objects": [entity_ref(obj, "object")],
                    "selections": [],
                },
                "arguments": {"object": entity_ref(obj, "object")},
            }
            receipt = await owner.analysis_command(
                {"notebookId": "nb", "command": inspect}
            )
            assert (await settled(owner, receipt["receipt"]["result"]["operationId"]))[
                "status"
            ] == "completed"
            fresh = owner.analysis.store.state_response(
                {"notebookId": "nb", "subject": {"kind": "object", "id": oid}}
            )["item"]
            state = owner.analysis.store.state_response({"notebookId": "nb"})
            remap = {
                "schemaVersion": 1,
                "commandId": "remap",
                "action": "selection.remap",
                "expected": {
                    "analysisRevision": state["analysisRevision"],
                    "kernels": [reference(kernel)],
                    "objects": [entity_ref(fresh, "object")],
                    "selections": [entity_ref(selected, "selection")],
                },
                "arguments": {
                    "selection": entity_ref(selected, "selection"),
                    "targetView": fresh["defaultView"],
                    "expectedMemberCount": 2,
                    "expectedMembersSha256": selected["membersSha256"],
                },
            }
            result = await owner.analysis_command(
                {"notebookId": "nb", "command": remap}
            )
            current = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {
                        "kind": "selection",
                        "id": result["receipt"]["result"]["entityId"],
                    },
                }
            )["item"]
            assert (
                current["valid"]
                and current["membersSha256"] == selected["membersSha256"]
            )
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("language", ["python", "r"])
def test_real_owner_collects_101_owned_ids_and_inspects_each_language(
    tmp_path, language
):
    import os
    import sys
    from pathlib import Path

    async def run():
        libraries = [
            p
            for p in os.environ.get("OMICSCLAW_TEST_R_LIBRARY", "").split(os.pathsep)
            if p
        ]
        if language == "r" and (
            not libraries or not Path(sys.executable).with_name("R").is_file()
        ):
            pytest.skip("Explicit R/IRkernel fixture requires OMICSCLAW_TEST_R_LIBRARY")
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, language, real=True, libraries=libraries)
            assert env["status"] == "ready", env
            kernel = (await bind(owner, language, env))["kernel"]
            source = (
                "import pandas as pd\ndata=pd.DataFrame({'value':range(101)},index=[f'cell_{i:03d}' for i in range(101)])\nnotebook_selection_ids(['index'], [[str(i)] for i in data.index])"
                if language == "python"
                else "data <- data.frame(value=0:100,row.names=sprintf('cell_%03d',0:100)); notebook_selection_ids(c('index'),as.list(rownames(data)))"
            )
            initial = await owner.admit(
                {"command": command(kernel, "create-ids", source)}
            )
            record = await settled(owner, initial["execution_id"])
            assert record["status"] == "completed", record
            inputs = owner.analysis.store.page(
                {"notebookId": "nb", "collection": "selectionInputs"}
            )["items"]
            assert len(inputs) == 1 and inputs[0]["count"] == 101
            assert inputs[0]["producerExecutionId"] == initial["execution_id"]
            spec = registration(kernel)
            state = owner.analysis.store.state_response({"notebookId": "nb"})
            spec["expected"]["analysisRevision"] = state["analysisRevision"]
            spec["arguments"].update(
                adapter="python_table" if language == "python" else "r_rds",
                rowIdentity={"kind": "index"},
                columnIdentity={"kind": "index"},
                declaredMatrix={"assay": None, "layer": None, "semantics": "unknown"},
            )
            receipt = await owner.analysis_command(
                {"notebookId": "nb", "command": spec}
            )
            inspected = await settled(
                owner, receipt["receipt"]["result"]["operationId"]
            )
            assert inspected["status"] == "completed", inspected
            obj = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {
                        "kind": "object",
                        "id": receipt["receipt"]["result"]["entityId"],
                    },
                }
            )["item"]
            assert obj["shape"] == [101, 1] and obj["availability"] == "verified", obj
            assert obj["kernel"]["language"] == language
            state = owner.analysis.store.state_response({"notebookId": "nb"})
            select = {
                "schemaVersion": 1,
                "commandId": "select-file",
                "action": "selection.create",
                "expected": {
                    "analysisRevision": state["analysisRevision"],
                    "kernels": [reference(kernel)],
                    "objects": [entity_ref(obj, "object")],
                    "selections": [],
                },
                "arguments": {
                    "view": obj["defaultView"],
                    "members": {
                        "kind": "idArtifact",
                        "artifact": {
                            key: inputs[0][key]
                            for key in ("artifactId", "sha256", "bytes")
                        },
                    },
                },
            }
            accepted = await owner.analysis_command(
                {"notebookId": "nb", "command": select}
            )
            selected = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {
                        "kind": "selection",
                        "id": accepted["receipt"]["result"]["entityId"],
                    },
                }
            )["item"]
            assert selected["count"] == 101 and selected["valid"]
            assert len(selected["sampleIds"]) == 20
            # Reading all rows advances a stable bounded cursor, preserving identity order.
            rows, cursor = [], None
            while True:
                page = owner.analysis.store.page(
                    {
                        "notebookId": "nb",
                        "collection": "viewRows",
                        "parentId": obj["defaultView"]["viewId"],
                        "viewRevision": obj["defaultView"]["viewRevision"],
                        "limit": 40,
                        **({"cursor": cursor} if cursor else {}),
                    }
                )
                rows += page["items"]
                cursor = page["nextCursor"]
                if cursor is None:
                    break
            assert [row["id"] for row in rows] == [
                [f"cell_{i:03d}"] for i in range(101)
            ]
            assert [row["values"][-1] for row in rows] == list(range(101))
            ref = {key: inputs[0][key] for key in ("artifactId", "sha256", "bytes")}
            with pytest.raises(NotebookError, match="invalid_reference"):
                owner.db.run(
                    lambda c: owner.analysis.files.validate_artifact(c, "foreign", ref)
                )
            artifact = owner.artifacts.get_artifact(ref["artifactId"])

            def corrupt():
                with Path(artifact.path).open("ab") as stream:
                    stream.write(b'["forged"]\n')

            await asyncio.to_thread(corrupt)
            with pytest.raises(NotebookError, match="reference_stale"):
                owner.db.run(
                    lambda c: owner.analysis.files.validate_artifact(c, "nb", ref)
                )
        finally:
            await owner.close()

    asyncio.run(run())
