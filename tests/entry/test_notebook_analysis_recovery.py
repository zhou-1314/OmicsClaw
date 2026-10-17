"""Explicit registration recovery preserves memory, history and selection intent."""

import asyncio
import copy

import pytest

from omicsclaw.entry.notebook.analysis_store import entity_ref
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_analysis_owner import reference, registration, settled
from tests.entry.test_notebook_r import bind, command, create, environment

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def current(owner, oid):
    return owner.analysis.store.state_response(
        {"notebookId": "nb", "subject": {"kind": "object", "id": oid}}
    )["item"]


def metadata(owner, action, arguments, cid, *, kernels=(), objects=(), selections=()):
    return {
        "notebookId": "nb",
        "command": {
            "schemaVersion": 1,
            "commandId": cid,
            "action": action,
            "arguments": arguments,
            "expected": {
                "analysisRevision": owner.analysis.store.state_response(
                    {"notebookId": "nb"}
                )["analysisRevision"],
                "kernels": list(kernels),
                "objects": list(objects),
                "selections": list(selections),
            },
        },
    }


async def execute(owner, kernel, cid, source):
    guard = owner.db.run(lambda c: owner.analysis.store.context(c, "nb"))["guards"][
        kernel["language"]
    ]
    result = await owner.admit(
        {
            "command": {
                **command(kernel, cid, source),
                "version": 3,
                "preconditions": {"analysis": guard},
            }
        }
    )
    result = await settled(owner, result["execution_id"])
    assert result["status"] == "completed", result
    return result


async def register(owner, kernel, cid, *, bad_layer=False):
    spec = registration(kernel, cid)
    spec["expected"]["analysisRevision"] = owner.analysis.store.state_response(
        {"notebookId": "nb"}
    )["analysisRevision"]
    spec["arguments"].update(
        adapter="anndata" if bad_layer else "python_table",
        rowIdentity={"kind": "index"},
        columnIdentity={"kind": "index"},
        declaredMatrix={
            "assay": None,
            "layer": "missing" if bad_layer else None,
            "semantics": "unknown",
        },
    )
    result = await owner.analysis_command({"notebookId": "nb", "command": spec})
    assert (await settled(owner, result["receipt"]["result"]["operationId"]))[
        "status"
    ] == "completed"
    return current(owner, result["receipt"]["result"]["entityId"])


@pytest.mark.parametrize("bad_layer", [False, True])
def test_real_remove_repairs_invalid_registration_without_losing_variable(
    tmp_path, bad_layer
):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, "python", real=True)
            kernel = (await bind(owner, "python", env))["kernel"]
            source = "import pandas as pd\ndata=pd.DataFrame({'value':[1,2]})\nidentity=id(data)"
            if bad_layer:
                source = "import anndata as ad, numpy as np\ndata=ad.AnnData(np.ones((2,2))); identity=id(data)"
            await execute(owner, kernel, "init", source)
            obj = await register(owner, kernel, "bad", bad_layer=bad_layer)
            assert (
                obj["registrationState"] == "active"
                and obj["availability"] == "needs_inspection"
            )
            with pytest.raises(NotebookError, match="reference_stale"):
                await execute(owner, kernel, "still-blocked", "must_not_run=True")
            ref = entity_ref(obj, "object")
            params = metadata(
                owner, "object.remove", {"object": ref}, "remove", objects=[ref]
            )
            receipt = await owner.analysis_command(params)
            removed = current(owner, obj["objectId"])
            assert (
                removed["registrationState"] == "removed"
                and removed["objectRevision"] == ref["objectRevision"] + 1
            )
            assert receipt["receipt"]["result"] == {
                "entityKind": "object",
                "entityId": obj["objectId"],
                "operationId": None,
            }
            assert (
                owner.actions.receipt({"notebookId": "nb", "commandId": "remove"})[
                    "receipt"
                ]
                == receipt["receipt"]
            )
            assert (await owner.analysis_command(params))["receipt"] == receipt[
                "receipt"
            ]
            changed = copy.deepcopy(params)
            changed["command"]["arguments"]["object"]["objectRevision"] += 1
            with pytest.raises(NotebookError, match="command_conflict"):
                await owner.analysis_command(changed)
            with pytest.raises(NotebookError, match="reference_stale"):
                await owner.analysis_command(
                    metadata(
                        owner,
                        "object.inspect",
                        {"object": entity_ref(removed, "object")},
                        "inspect-removed",
                        kernels=[reference(kernel)],
                        objects=[entity_ref(removed, "object")],
                    )
                )
            repair = (
                "assert id(data)==identity\ndata.index=['a','b']"
                if not bad_layer
                else "assert id(data)==identity\ndata.layers['missing']=data.X.copy()"
            )
            await execute(owner, kernel, "repair", repair)
            fresh = await register(owner, kernel, "fixed", bad_layer=bad_layer)
            assert (
                fresh["objectId"] != obj["objectId"]
                and fresh["availability"] == "verified"
            )
            assert current(owner, obj["objectId"])["registrationState"] == "removed"
            historical = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {"kind": "object", "id": obj["objectId"], "revision": 1},
                }
            )["item"]
            assert (
                historical["symbol"] == "data"
                and historical["registrationState"] == "removed"
            )
            await execute(owner, kernel, "continue", "assert id(data)==identity")
        finally:
            await owner.close()

    asyncio.run(run())


def test_real_new_generation_initialization_ignores_old_objects_but_keeps_selection(
    tmp_path,
):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, "python", real=True)
            kernel = (await bind(owner, "python", env))["kernel"]
            await execute(
                owner,
                kernel,
                "init",
                "import pandas as pd\ndata=pd.DataFrame({'value':[1,2]},index=['a','b'])",
            )
            obj = await register(owner, kernel, "old-register")
            receipt = await owner.analysis_command(
                metadata(
                    owner,
                    "selection.create",
                    {
                        "view": obj["defaultView"],
                        "members": {"kind": "ids", "values": [["a"]]},
                    },
                    "select",
                    kernels=[reference(kernel)],
                    objects=[entity_ref(obj, "object")],
                )
            )
            selected = {
                "selectionId": receipt["receipt"]["result"]["entityId"],
                "selectionRevision": 1,
            }
            await owner.release(
                {
                    "notebookId": "nb",
                    "commandId": "release",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": kernel["generation"],
                }
            )
            new = (await bind(owner, "python", env, "new"))["kernel"]
            assert current(owner, obj["objectId"])["registrationState"] == "historical"
            with pytest.raises(
                NotebookError, match="reference_stale|selection_invalid"
            ):
                await execute(owner, new, "selected-old", "must_not_run=True")
            await owner.analysis_command(
                metadata(
                    owner,
                    "selection.clear",
                    {"selection": selected},
                    "clear",
                    selections=[selected],
                )
            )
            await execute(
                owner,
                new,
                "reinitialize",
                "import pandas as pd\ndata=pd.DataFrame({'value':[3,4]},index=['a','b'])",
            )
            fresh = await register(owner, new, "new-register")
            assert (
                fresh["objectId"] != obj["objectId"]
                and fresh["registrationState"] == "active"
            )
            assert current(owner, obj["objectId"])["registrationState"] == "historical"
            old_selection = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {"kind": "selection", "id": selected["selectionId"]},
                }
            )["item"]
            assert old_selection["valid"] is False
            await execute(owner, new, "new-continue", "assert data.value.sum()==7")
        finally:
            await owner.close()

    asyncio.run(run())
