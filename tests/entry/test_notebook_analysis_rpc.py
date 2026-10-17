"""The SSH controller exposes fixed state actions and preserves target fences."""

import asyncio
from types import SimpleNamespace

import pytest

from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.ssh.protocol import PUBLIC_ERROR_CODES, PublicRpcError
from tests.entry.test_notebook_r import create
from tests.entry.test_ssh_notebook_binding import Client

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def test_configured_controller_mounts_metadata_tool_and_all_three_rpc_routes(tmp_path):
    async def run():
        peer = SimpleNamespace(handlers={})
        controller = Controller(tmp_path, peer)
        owner = await create(tmp_path)

        class Adapter(Client):
            async def call(self, method, params, **kwargs):
                if method == "analysis_state":
                    return owner.analysis.store.state_response(params)
                if method == "analysis_page":
                    return owner.analysis.store.page(params)
                return await super().call(method, params, **kwargs)

            async def aclose(self):
                pass

        try:
            await controller.configure(
                {
                    "provider_id": "test",
                    "dialect": "openai_chat",
                    "model": "test",
                    "allowed_models": ["test"],
                    "context_tokens": 8192,
                    "output_tokens": 1024,
                }
            )
            assert controller.app.registry.get("notebook_analysis") is not None
            await controller.notebook_client.aclose()
            controller.notebook_client = Adapter(owner)
            base = controller._environment_target()
            scope = {key: base[key] for key in ("authorityId", "workspaceId")}
            state = await peer.handlers["notebooks.analysisState"](
                {"scope": scope, "notebookId": "nb"}
            )
            assert state["analysisRevision"] == 0
            current = await peer.handlers["notebooks.analysisState"](
                {"scope": scope, "notebookId": "nb", "subject": {"kind": "runContext"}}
            )
            assert current["item"] == {
                "kernels": [],
                "objects": [],
                "activeSelection": None,
                "selectionObject": None,
            }
            assert current["analysisRevision"] == state["analysisRevision"]
            checkpoint = await peer.handlers["notebooks.analysisState"](
                {
                    "scope": scope,
                    "notebookId": "nb",
                    "subject": {"kind": "checkpointOverview"},
                }
            )
            assert checkpoint["item"]["continuationReady"]
            assert (
                checkpoint["item"]["limits"]["deadlineMode"]
                == "interrupt_without_kernel_kill"
            )
            page = await peer.handlers["notebooks.analysisPage"](
                {"scope": scope, "notebookId": "nb", "collection": "checkpoints"}
            )
            assert page["items"] == [] and page["parentRevision"] is None
            restore = await peer.handlers["notebooks.analysisState"](
                {
                    "scope": scope,
                    "notebookId": "nb",
                    "subject": {"kind": "checkpointRestoreOverview"},
                }
            )
            assert restore["analysisRevision"] == state["analysisRevision"]
            assert restore["item"]["continuationReady"]
            for language in ("python", "r"):
                current = restore["item"]["languages"][language]
                assert current["kernel"] is None
                assert current["activeOperationId"] is None
                assert current["eligible"] is False
                assert current["canRun"] and current["canInspect"]
            restores = await peer.handlers["notebooks.analysisPage"](
                {
                    "scope": scope,
                    "notebookId": "nb",
                    "collection": "checkpointRestoreOperations",
                }
            )
            assert restores["items"] == []
            assert restores["parentId"] is None
            assert restores["parentRevision"] is None
            assert restores["nextCursor"] is None
            assert (
                await peer.handlers["notebooks.analysisPage"](
                    {"scope": scope, "notebookId": "nb", "collection": "objects"}
                )
            )["items"] == []
            target = {**base, "ownerId": owner.owner_id, "ownerEpoch": owner.epoch}
            with pytest.raises(
                PublicRpcError, match="notebook_analysis_invalid_command"
            ):
                await peer.handlers["notebooks.analysisCommand"](
                    {"target": target, "notebookId": "nb", "command": {}}
                )
            with pytest.raises(PublicRpcError, match="notebook_target_changed"):
                await peer.handlers["notebooks.analysisCommand"](
                    {
                        "target": {**target, "ownerEpoch": owner.epoch + 1},
                        "notebookId": "nb",
                        "command": {},
                    }
                )
            with pytest.raises(PublicRpcError, match="notebook_target_changed"):
                await peer.handlers["notebooks.analysisState"](
                    {"scope": {**scope, "workspaceId": "foreign"}, "notebookId": "nb"}
                )
            assert "notebook_analysis_state_v1" in owner.health()["capabilities"]
            assert "notebook_analysis_run_context_v1" in owner.health()["capabilities"]
            assert "notebook_checkpoints_v1" in owner.health()["capabilities"]
            assert (
                "environment_checkpoint_maintenance_v1"
                not in owner.health()["capabilities"]
            )
            assert (
                "notebook_checkpoint_restore_v1" in owner.health()["capabilities"]
            )
            assert "notebook_checkpoint_invalid_command" in PUBLIC_ERROR_CODES
            assert "notebook_checkpoint_private_detail" not in PUBLIC_ERROR_CODES
            assert "notebook_analysis_invalid_command" in PUBLIC_ERROR_CODES
            assert "notebook_analysis_private_detail" not in PUBLIC_ERROR_CODES
        finally:
            await owner.close()
            await controller.aclose()

    asyncio.run(run())
