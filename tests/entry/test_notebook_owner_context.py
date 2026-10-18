"""Fresh model requests are checked against the owner's durable state."""

import asyncio

import pytest

from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.service import NotebookError
from tests.entry.test_notebook_owner import Manager, observation, setup, settled


async def fresh_runtime(tmp_path):
    runtime = OwnerRuntime(
        tmp_path,
        {"authorityId": "a" * 32, "workspaceId": "b" * 32},
        source_digest="fixture-component",
        env={},
        manager=Manager(),
        registry_probe=observation,
    )
    await runtime.start()
    return runtime


async def register(runtime, path):
    return await runtime.registry.register(
        {
            "language": "python",
            "executable": path,
            "libraryPaths": [],
            "requiredPackages": [],
        }
    )


@pytest.mark.usefixtures("notebook_control_rss")
def test_unbound_selection_zero_one_many_and_invalid_default_never_falls_back(tmp_path):
    async def run():
        r = await fresh_runtime(tmp_path)
        try:

            async def context():
                return await r.context({"sessionId": "chat", "requestId": "request"})

            assert (await context())["summary"]["selection"][
                "reason"
            ] == "notebook_environment_required"
            first = await register(r, "/fixture/python1")
            one = await context()
            assert (
                one["summary"]["selection"]["environmentId"] == first["environmentId"]
            )
            assert one["summary"]["binding"] is None
            assert r.manager.codes == []
            await r.check_request(
                {"sessionId": "chat", "requestId": "request", "token": one["token"]}
            )
            second = await register(r, "/fixture/python2")
            assert (await context())["summary"]["selection"][
                "reason"
            ] == "notebook_environment_ambiguous"
            with pytest.raises(NotebookError, match="notebook_request_stale"):
                await r.check_request(
                    {"sessionId": "chat", "requestId": "request", "token": one["token"]}
                )
            await r.select_environment(
                {
                    "commandId": "select",
                    "expectedRevision": 0,
                    "environmentId": first["environmentId"],
                    "environmentRevision": first["revision"],
                }
            )
            assert not (await context())["summary"]["selection"]["reason"]
            r.registry_db.run(
                lambda c: c.execute(
                    "UPDATE registered_environments SET status='unverified' WHERE environment_id=?",
                    (first["environmentId"],),
                )
            )
            selected = (await context())["summary"]["selection"]
            assert selected["reason"] == "notebook_environment_invalid"
            assert (
                selected["environmentId"]
                == first["environmentId"]
                != second["environmentId"]
            )
            assert not r.views.listing()["items"] and not r.manager.codes
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_snapshot_tokens_are_private_session_request_and_state_bound(
    tmp_path, monkeypatch
):
    async def run():
        r, manager, command = await setup(tmp_path, monkeypatch)
        try:
            snap = await r.context({"sessionId": "chat", "requestId": "request"})
            check = {
                "sessionId": "chat",
                "requestId": "request",
                "token": snap["token"],
            }
            await r.check_request(check)
            for changed in (
                {"sessionId": "other"},
                {"requestId": "other"},
                {"token": snap["token"] + "x"},
            ):
                with pytest.raises(NotebookError, match="notebook_request_invalid"):
                    await r.check_request({**check, **changed})
            record = await r.admit({"command": command})
            await settled(r, record["execution_id"])
            with pytest.raises(NotebookError, match="notebook_request_stale"):
                await r.check_request(check)
            after = await r.context({"sessionId": "chat", "requestId": "next"})
            assert after["summary"]["state"]["stateSeq"] == 1
            # The pending and committed output writes count as one outcome.
            assert r.views.snapshot("nb").get("token") is None
            assert (
                r.db.run(
                    lambda c: c.execute(
                        "SELECT source_digest FROM notebook_component_provenance WHERE kind='execution'"
                    ).fetchone()
                )[0]
                == "fixture"
            )
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_takeover_in_the_final_transaction_refuses_an_earlier_request(
    tmp_path, monkeypatch
):
    async def run():
        r, _, _ = await setup(tmp_path, monkeypatch)
        try:
            snap = await r.context({"sessionId": "chat", "requestId": "request"})
            await r.check_request(
                {"sessionId": "chat", "requestId": "request", "token": snap["token"]}
            )

            def admission(c):
                r.requests.takeover(c, "nb")
                r.requests.verify(c, snap["token"], "chat", "request")

            with pytest.raises(NotebookError, match="notebook_request_stale"):
                r.service._transaction(admission)
            # Both writes are rolled back by the original transaction boundary.
            assert (await r.context({"sessionId": "chat", "requestId": "after"}))[
                "summary"
            ]["state"]["userWatermark"] == 0
            r.service._transaction(lambda c: r.requests.takeover(c, "nb"))
            with pytest.raises(NotebookError, match="notebook_request_stale"):
                await r.check_request(
                    {
                        "sessionId": "chat",
                        "requestId": "request",
                        "token": snap["token"],
                    }
                )
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_multi_conda_environments_auto_resolve_zero_click_and_export_display_names(
    tmp_path,
):
    async def run():
        r = await fresh_runtime(tmp_path)
        try:
            base_env = await register(r, "/opt/conda/bin/python")
            scrna_env = await register(
                r, "/root/.claude-science/conda/envs/scrnaseq/bin/python"
            )
            omics_env = await register(
                r, "/opt/conda/envs/OmicsClaw/bin/python"
            )
            assert base_env["displayName"] == "base"
            assert scrna_env["displayName"] == "scrnaseq"
            assert omics_env["displayName"] == "OmicsClaw"

            ctx = await r.context({"sessionId": "chat", "requestId": "req-multi-1"})
            selection = ctx["summary"]["selection"]
            assert selection["reason"] == ""
            assert selection["environmentId"] == omics_env["environmentId"]
            assert selection["displayName"] == "OmicsClaw"
            assert [c["displayName"] for c in selection["candidates"]] == [
                "OmicsClaw",
                "scrnaseq",
                "base",
            ]

            await r.check_request(
                {
                    "sessionId": "chat",
                    "requestId": "req-multi-1",
                    "token": ctx["token"],
                }
            )
            import hashlib
            nid = hashlib.sha256(b"notebook:chat").hexdigest()
            kid = hashlib.sha256(b"kernel:chat:req-multi-1").hexdigest()[:32]
            record = await r.admit_agent(
                {
                    "command": {
                        "version": 4,
                        "notebookId": nid,
                        "cellId": "cell-multi-1",
                        "commandId": "cmd-multi-1",
                        "source": "x = 42",
                        "expectedRevision": 0,
                        "language": "python",
                        "origin": "agent",
                        "approvalScope": "permission_gate",
                        "timeoutSeconds": 30,
                        "kernelInstanceId": kid,
                        "generation": 1,
                        "environmentId": selection["environmentId"],
                        "environmentRevision": selection["environmentRevision"],
                        "preconditions": {
                            "sessionId": "chat",
                            "requestId": "req-multi-1",
                            "token": ctx["token"],
                            "toolPosition": 0,
                            "analysis": ctx["summary"]["analysis"]["guards"]["python"],
                        },
                    }
                }
            )
            for _ in range(100):
                row = r.views.execution(nid, record["execution_id"])
                if row["status"] == "completed" and row["durability"] == "committed":
                    break
                await asyncio.sleep(0.01)
            snap = r.views.snapshot(nid)
            assert snap["kernel"]["displayName"] == "OmicsClaw"
            assert snap["kernels"]["python"]["displayName"] == "OmicsClaw"
        finally:
            await r.close()

    asyncio.run(run())
