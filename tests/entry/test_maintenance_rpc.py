"""Maintenance refusals and observation scopes cross both actual JSON transports."""

import asyncio
import itertools
import json
import os

from omicsclaw.entry.environment_coordination import EnvironmentCoordinator
from omicsclaw.entry.notebook.policy import decode_policy
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.security import process_evidence, write_private
from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.ssh.notebook_ipc import OWNER_PROTOCOL, OwnerServer
from omicsclaw.entry.ssh.protocol import JsonPeer


def test_actual_peer_controller_owner_ipc_preserves_semantic_refusals(
    tmp_path, monkeypatch
):
    async def run():
        reader, responses = asyncio.StreamReader(), asyncio.Queue()

        async def write(raw):
            await responses.put(json.loads(raw))

        peer = JsonPeer(reader, write)
        controller = Controller(tmp_path, peer)
        runtime = server = peer_task = None
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
            client = controller.notebook_client
            runtime = OwnerRuntime(
                tmp_path,
                client.identity,
                source_digest="rpc-fixture",
                env={},
                config=decode_policy(client.policy),
                environment_coordinator=EnvironmentCoordinator(tmp_path / "global"),
            )
            await runtime.start()
            address = "omicsclaw-notebook-test-" + os.urandom(8).hex()
            server = await asyncio.start_unix_server(
                OwnerServer(runtime).handle, path="\0" + address
            )
            write_private(
                client.directory / "owner.json",
                {
                    "process": process_evidence(os.getpid()),
                    "identity": client.identity,
                    "protocol": OWNER_PROTOCOL,
                    "address": address,
                },
            )
            health = await client.attach()
            assert "environment_maintenance_v1" in health["capabilities"]
            assert (await controller.environment_list({}))["maintenance"] == {
                "enabled": True,
                "reason": "",
            }
            target = {
                **controller._environment_target(),
                **{k: health["owner"][k] for k in ("ownerId", "ownerEpoch")},
            }
            scope = dict(client.identity)
            peer_task = asyncio.create_task(peer.run())
            identities = itertools.count()

            async def request(method, params):
                identity = "probe-" + str(next(identities))
                reader.feed_data(
                    json.dumps(
                        {
                            "kind": "request",
                            "id": identity,
                            "method": method,
                            "params": params,
                        }
                    ).encode()
                    + b"\n"
                )
                answer = await asyncio.wait_for(responses.get(), 5)
                assert answer["id"] == identity
                return {**answer, "id": "probe"}

            answer = await request(
                "environments.maintenanceReceipt",
                {"scope": scope, "commandId": "missing"},
            )
            assert answer["result"] == {"found": False, "receipt": None}
            for action, params, code in [
                (
                    "Plan",
                    {"target": target, "commandId": "bad-plan"},
                    "environment_maintenance_invalid_request",
                ),
                (
                    "Events",
                    {"scope": scope, "maintenanceId": "missing", "limit": True},
                    "environment_maintenance_invalid_page",
                ),
                (
                    "Get",
                    {"scope": scope, "maintenanceId": "missing"},
                    "environment_maintenance_not_found",
                ),
                (
                    "Receipt",
                    {"scope": {**scope, "workspaceId": "old"}, "commandId": "missing"},
                    "environment_target_changed",
                ),
                (
                    "Cancel",
                    {"target": {**target, "ownerEpoch": 0}, "commandId": "old"},
                    "environment_target_changed",
                ),
            ]:
                assert await request("environments.maintenance" + action, params) == {
                    "kind": "response",
                    "id": "probe",
                    "error": {"code": code},
                }

            async def unexpected(action, params):
                raise RuntimeError("private=must-never-cross-IPC")

            monkeypatch.setattr(runtime.maintenance_service, "call", unexpected)
            assert await request("environments.maintenanceList", {"scope": scope}) == {
                "kind": "response",
                "id": "probe",
                "error": {"code": "notebook_owner_unavailable"},
            }
        finally:
            reader.feed_eof()
            if peer_task:
                await peer_task
            if server:
                server.close()
                await server.wait_closed()
            await controller.aclose()
            if runtime:
                await runtime.close()

    asyncio.run(run())
