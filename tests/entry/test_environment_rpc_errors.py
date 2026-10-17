"""Only declared environment refusals survive the controller's wire redaction."""

import asyncio
import json
import sys

import pytest

from omicsclaw.entry.ssh.controller import Controller
from omicsclaw.entry.ssh.protocol import JsonPeer, PublicRpcError, PUBLIC_ERROR_CODES


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("invalid", "invalid_environment_path"),
        ("revision", "environment_revision_conflict"),
        ("target", "environment_target_changed"),
        ("unknown", "request_refused"),
        ("unavailable", "environment_registry_unavailable"),
    ],
)
def test_real_peer_loop_preserves_only_allowlisted_environment_codes(
    tmp_path, scenario, code
):
    async def run():
        reader = asyncio.StreamReader()
        frames = []

        async def write(raw):
            frames.append(json.loads(raw))
            reader.feed_eof()

        peer = JsonPeer(reader, write)
        controller = Controller(tmp_path, peer)
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
            target = controller._environment_target()
            if scenario == "invalid":
                method = "environments.register"
                params = {
                    "target": target,
                    "spec": {
                        "language": "python",
                        "executable": "relative",
                        "libraryPaths": [],
                        "requiredPackages": [],
                    },
                }
            elif scenario == "revision":
                row = await controller.environments.register(
                    {
                        "language": "python",
                        "executable": sys.executable,
                        "libraryPaths": [],
                        "requiredPackages": [],
                    }
                )
                method = "environments.accept"
                params = {
                    "target": target,
                    "environmentId": row["environmentId"],
                    "revision": 0,
                }
            elif scenario == "target":
                method, params = (
                    "environments.preflight",
                    {
                        "target": {**target, "controllerEpoch": "old"},
                        "environmentId": "0" * 32,
                    },
                )
            elif scenario == "unavailable":
                controller.environments = None
                method, params = "environments.list", {}
            else:

                async def unknown(_params):
                    raise RuntimeError("credential=never-emit-this")

                peer.handlers["fixture.unknown"] = unknown
                method, params = "fixture.unknown", {}
            reader.feed_data(
                json.dumps(
                    {
                        "kind": "request",
                        "id": "fixture",
                        "method": method,
                        "params": params,
                    }
                ).encode()
                + b"\n"
            )
            await peer.run()
            assert frames == [
                {"kind": "response", "id": "fixture", "error": {"code": code}}
            ]
        finally:
            await controller.aclose()

    asyncio.run(run())


def test_public_error_type_does_not_accept_arbitrary_text():
    assert (
        PublicRpcError("environment_target_changed secret=hidden").code
        == "request_refused"
    )
    assert all(len(code) < 80 and code.isascii() for code in PUBLIC_ERROR_CODES)
