"""Detached native egress and artifact ownership across controller shutdown."""

import asyncio
import dataclasses
import signal
import sys
from types import SimpleNamespace

import pytest

from omicsclaw.entry.notebook.security import same_process
from omicsclaw.entry.ssh import controller as controller_module
from omicsclaw.sandbox import EgressMode, SandboxMode, probe_sandbox_backend


def test_owner_proxy_and_artifact_sink_survive_controller_shutdown(tmp_path):
    pytest.importorskip("ipykernel")
    pytest.importorskip("matplotlib")
    if not probe_sandbox_backend().available:
        pytest.skip("native network sandbox unavailable on this host")
    from tests.entry.notebook_process_fixture import run_controller_scenario

    run_controller_scenario(__name__, "_native_owner_scenario", tmp_path)


def _native_owner_scenario(tmp_path):
    with pytest.MonkeyPatch.context() as monkeypatch:
        _native_owner_with_patch(tmp_path, monkeypatch)


def _native_owner_with_patch(tmp_path, monkeypatch):

    async def run():
        async def http(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\napproved"
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(http, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        original = controller_module.AppConfig

        def configured(**kwargs):
            return dataclasses.replace(
                original(**kwargs),
                sandbox=SandboxMode.AUTO,
                sandbox_egress=EgressMode.PROXY,
                sandbox_egress_endpoints=(f"127.0.0.1:{port}",),
            )

        monkeypatch.setattr(controller_module, "AppConfig", configured)

        async def connect():
            client = controller_module.Controller(
                tmp_path, SimpleNamespace(handlers={})
            )
            await client.configure(
                {
                    "provider_id": "test",
                    "dialect": "openai_chat",
                    "model": "test",
                    "allowed_models": ["test"],
                    "context_tokens": 8192,
                    "output_tokens": 1024,
                }
            )
            return client

        first = await connect()
        second = None
        health = None
        try:
            env = (
                await first.environment_register(
                    {
                        "target": first._environment_target(),
                        "spec": {
                            "language": "python",
                            "executable": sys.executable,
                            "libraryPaths": [],
                            "requiredPackages": [],
                        },
                    }
                )
            )["environment"]
            health = await first.notebook_request(
                "attach", {"target": first._environment_target()}
            )
            bound = await first.notebook_request(
                "bind",
                {
                    "target": health["writeFence"],
                    "notebookId": "nb",
                    "sessionId": "chat",
                    "commandId": "bind",
                    "environmentId": env["environmentId"],
                    "environmentRevision": env["revision"],
                },
            )
            marker = tmp_path / "before-detach"
            resume = tmp_path / "resume"
            source = f"""import os,time,urllib.request,urllib.error
from pathlib import Path
os.environ.pop('NO_PROXY',None)
os.environ.pop('no_proxy',None)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({{'http':os.environ['HTTP_PROXY']}}))
print(opener.open('http://127.0.0.1:{port}/',timeout=5).read().decode(),flush=True)
Path({str(marker)!r}).write_text('ready')
while not Path({str(resume)!r}).exists(): time.sleep(.05)
print(opener.open('http://127.0.0.1:{port}/',timeout=5).read().decode(),flush=True)
try:
 opener.open('http://unapproved.example.com/',timeout=5)
except urllib.error.HTTPError as error:
 assert error.code==403
 print('blocked',flush=True)
else:
 raise AssertionError('unapproved network allowed')
import matplotlib.pyplot as plt
plt.plot([0,1],[0,1])
plt.show()
"""
            doc = {
                "version": 1,
                "notebookId": "nb",
                "cellId": "cell",
                "commandId": "run",
                "source": source,
                "expectedRevision": 0,
                "language": "python",
                "origin": "user",
                "approvalScope": "explicit_user",
                "timeoutSeconds": 30,
                "preconditions": {},
                **{
                    k: bound["kernel"][k]
                    for k in (
                        "kernelInstanceId",
                        "generation",
                        "environmentId",
                        "environmentRevision",
                    )
                },
            }
            receipt = await first.notebook_request(
                "admit", {"target": health["writeFence"], "command": doc}
            )
            for _ in range(300):
                if marker.exists():
                    break
                await asyncio.sleep(0.05)
            assert marker.exists()
            await first.aclose()
            assert same_process(health["owner"]["process"])
            resume.write_text("continue")
            second = await connect()
            newer = await second.notebook_request(
                "attach", {"target": second._environment_target()}
            )
            assert newer["owner"] == health["owner"]
            assert (
                newer["sandbox"]["egress"] == "proxy"
                and newer["sandbox"]["kernelIsolation"]
            )
            for _ in range(400):
                result = await second.notebook_request(
                    "execution",
                    {
                        "target": newer["writeFence"],
                        "notebookId": "nb",
                        "executionId": receipt["execution_id"],
                    },
                )
                if (
                    result["status"] == "completed"
                    and result["durability"] == "committed"
                ):
                    break
                await asyncio.sleep(0.05)
            assert result["status"] == "completed", result
            assert result["output"]["stdout"].count("approved") == 2
            assert "blocked" in result["output"]["stdout"]
            assert result["output"]["figures"][0]["artifact_id"]
            await second.notebook_request(
                "release",
                {
                    "target": newer["writeFence"],
                    "notebookId": "nb",
                    "commandId": "release",
                    "kernelInstanceId": bound["kernel"]["kernelInstanceId"],
                    "generation": 1,
                },
            )
        finally:
            if second:
                await second.aclose()
            elif not first.notebook_client.closed:
                await first.aclose()
            if health and same_process(health["owner"]["process"]):
                import os

                os.kill(health["owner"]["process"]["pid"], signal.SIGTERM)
                for _ in range(100):
                    if not same_process(health["owner"]["process"]):
                        break
                    await asyncio.sleep(0.05)
                assert not same_process(health["owner"]["process"])
            server.close()
            await server.wait_closed()

    asyncio.run(run())
