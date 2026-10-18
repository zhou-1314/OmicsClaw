"""Ordered MIME output and authorized image chunks over a real owner kernel."""

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

from omicsclaw.entry.notebook.binding import SSHNotebookBinding
from omicsclaw.entry.notebook.contracts import encoded
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.schema import ToolCall
from omicsclaw.tools.context import use_tool_context
from tests.entry.test_notebook_owner_context import register
from tests.entry.test_ssh_notebook_binding import Client, registry

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWM8AAAAASUVORK5CYII="
pytestmark = pytest.mark.usefixtures("notebook_control_rss")


def test_real_ordered_outputs_match_events_and_image_chunks_are_versioned(tmp_path):
    pytest.importorskip("ipykernel")

    async def run():
        runtime = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="rich",
            env={},
        )
        await runtime.start()
        try:
            await register(runtime, sys.executable)
            binding = SSHNotebookBinding(Client(runtime))
            tools = registry(binding)
            source = f"""from IPython.display import display, HTML, Image, clear_output
import base64
print("first")
display(HTML('<table><tr><th>gene</th><th>count</th></tr><tr><td>A</td><td>3</td></tr></table>'))
display(Image(data=base64.b64decode({PNG!r})))
handle = display('before', display_id='progress')
handle.update('after')
clear_output(wait=True)
42
"""
            call = ToolCall(
                id="rich", name="python", arguments=json.dumps({"code": source})
            )

            async def approve(request):
                return True

            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                prepared = await binding.notebook_augmentor("chat").prepare_request(
                    (), (), "request"
                )
                with tools.use_request_context(prepared.context, 0, call):
                    response = await tools.execute(call)
            assert not response.is_error, response.output
            result = json.loads(response.output)
            assert result["status"] == "completed", result
            outputs = result["output"]["outputs"]
            assert [item["type"] for item in outputs] == [
                "stream",
                "table",
                "image",
                "text",
                "update",
                "clear",
                "text",
            ]
            assert outputs[-1]["text"] == "42"
            assert outputs[1]["rows"] == [["A", "3"]]
            nid, eid = result["notebook_id"], result["execution_id"]
            events = runtime.views.events(nid, eid, limit=40)["items"]
            assert [
                event["payload"]["output"]
                for event in events
                if event["type"] == "notebook_output"
            ] == outputs
            params = {
                "notebookId": nid,
                "executionId": eid,
                "artifactId": outputs[2]["artifactId"],
            }
            info = runtime.artifact_reader.read(params)
            chunks = []
            offset = 0
            while True:
                chunk = runtime.artifact_reader.read(
                    {
                        **params,
                        "version": info["version"],
                        "offset": offset,
                        "length": 13,
                    },
                    chunk=True,
                )
                chunks.append(base64.b64decode(chunk["dataBase64"]))
                offset = chunk["nextOffset"]
                if chunk["eof"]:
                    break
            assert hashlib.sha256(b"".join(chunks)).hexdigest() == info["sha256"]
            with pytest.raises(NotebookError, match="notebook_artifact_unavailable"):
                runtime.artifact_reader.read({**params, "notebookId": "other"})
            path = Path(runtime.artifacts.get_artifact(params["artifactId"]).path)
            path.write_bytes(path.read_bytes() + b"changed")
            with pytest.raises(NotebookError, match="notebook_artifact_changed"):
                runtime.artifact_reader.read(
                    {**params, "version": info["version"], "offset": 0, "length": 13},
                    chunk=True,
                )
            path.unlink()
            path.symlink_to(tmp_path / ".omicsclaw" / "memory.db")
            with pytest.raises(NotebookError, match="notebook_artifact_unavailable"):
                runtime.artifact_reader.read(params)
            assert len(encoded(result["output"])) < 65536
        finally:
            await runtime.close()

    asyncio.run(run())


def test_real_large_unicode_error_is_bounded_and_user_failure_keeps_mutation(tmp_path):
    pytest.importorskip("ipykernel")

    async def run():
        r = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="bounds",
            env={},
        )
        await r.start()
        try:
            await register(r, sys.executable)
            binding = SSHNotebookBinding(Client(r))
            tools = registry(binding)

            async def agent(code, request):
                call = ToolCall(
                    id=request, name="python", arguments=json.dumps({"code": code})
                )

                async def approve(request):
                    return True

                with use_tool_context(approval=approve, values={"session_id": "chat"}):
                    prepared = await binding.notebook_augmentor("chat").prepare_request(
                        (), (), request
                    )
                    with tools.use_request_context(prepared.context, 0, call):
                        result = await tools.execute(call)
                assert not result.is_error, result.output
                return json.loads(result.output)

            first = await agent("shared = [1]", "first")
            nid = first["notebook_id"]
            kernel = r.views.snapshot(nid)["kernel"]
            receipt = await r.admit(
                {
                    "command": {
                        "version": 1,
                        "notebookId": nid,
                        "cellId": "failed-user",
                        "commandId": "failed-user",
                        "source": "shared.append(2)\nprint('汉\\\"' * 600000)\nraise ValueError('组学' * 600000)",
                        "expectedRevision": 0,
                        "language": "python",
                        "origin": "user",
                        "approvalScope": "explicit_user",
                        "timeoutSeconds": 30,
                        "preconditions": {},
                        **{
                            k: kernel[k]
                            for k in (
                                "kernelInstanceId",
                                "generation",
                                "environmentId",
                                "environmentRevision",
                            )
                        },
                    }
                }
            )
            for _ in range(1500):
                row = r.views.execution(nid, receipt["execution_id"])
                if row["durability"] != "pending":
                    break
                await asyncio.sleep(0.01)
            assert row["status"] == "failed" and row["durability"] == "committed", row
            assert len(encoded(row)) < 256 * 1024
            assert len(encoded(row["output"]["outputs"])) <= 24576
            assert "truncated" in json.dumps(row["output"])
            snapshot = r.views.snapshot(nid)
            assert snapshot["control"]["stateSeq"] == 2
            assert snapshot["kernel"]["kernelInstanceId"] == kernel["kernelInstanceId"]
            await r.resume(
                {
                    "notebookId": nid,
                    "commandId": "resume",
                    "userWatermark": 1,
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": 1,
                }
            )
            result = await agent("print(shared)", "inspect")
            assert result["output"]["stdout"] == "[1, 2]\n"
        finally:
            await r.close()

    asyncio.run(run())



def test_same_path_figure_overwrite_across_cells_preserves_both_artifacts(tmp_path):
    pytest.importorskip("ipykernel")

    async def run():
        runtime = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="rich-overwrite",
            env={},
        )
        await runtime.start()
        try:
            await register(runtime, sys.executable)
            binding = SSHNotebookBinding(Client(runtime))
            tools = registry(binding)

            async def approve(request):
                return True

            async def run_cell(code_str, req_id):
                call = ToolCall(
                    id=req_id, name="python", arguments=json.dumps({"code": code_str})
                )
                with use_tool_context(approval=approve, values={"session_id": "chat-overwrite"}):
                    prepared = await binding.notebook_augmentor("chat-overwrite").prepare_request(
                        (), (), req_id
                    )
                    with tools.use_request_context(prepared.context, 0, call):
                        response = await tools.execute(call)
                assert not response.is_error, response.output
                return json.loads(response.output)

            png1 = base64.b64decode(PNG)
            # Construct a second distinct valid PNG (append a harmless tEXt/trailing chunk or use matplotlib if available, or distinct valid PNG bytes)
            png2 = png1 + b"second-cell-figure-payload"
            res1 = await run_cell(
                f"from pathlib import Path\nPath('figures').mkdir(exist_ok=True)\nPath('figures/qc_distributions.png').write_bytes({png1!r})",
                "req-cell-1",
            )
            res2 = await run_cell(
                f"from pathlib import Path\nPath('figures/qc_distributions.png').write_bytes({png2!r})",
                "req-cell-2",
            )
            art1 = res1["output"]["figures"][0]["artifact_id"]
            art2 = res2["output"]["figures"][0]["artifact_id"]
            assert art1 != art2

            info1 = runtime.artifact_reader.read(
                {
                    "notebookId": res1["notebook_id"],
                    "executionId": res1["execution_id"],
                    "artifactId": art1,
                }
            )
            info2 = runtime.artifact_reader.read(
                {
                    "notebookId": res2["notebook_id"],
                    "executionId": res2["execution_id"],
                    "artifactId": art2,
                }
            )
            assert info1["size"] == len(png1)
            assert info1["sha256"] == hashlib.sha256(png1).hexdigest()
            assert info2["size"] == len(png2)
            assert info2["sha256"] == hashlib.sha256(png2).hexdigest()
            chunk1 = runtime.artifact_reader.read(
                {
                    "notebookId": res1["notebook_id"],
                    "executionId": res1["execution_id"],
                    "artifactId": art1,
                    "version": info1["version"],
                    "offset": 0,
                    "length": len(png1),
                },
                chunk=True,
            )
            chunk2 = runtime.artifact_reader.read(
                {
                    "notebookId": res2["notebook_id"],
                    "executionId": res2["execution_id"],
                    "artifactId": art2,
                    "version": info2["version"],
                    "offset": 0,
                    "length": len(png2),
                },
                chunk=True,
            )
            assert base64.b64decode(chunk1["dataBase64"]) == png1
            assert base64.b64decode(chunk2["dataBase64"]) == png2
        finally:
            await runtime.close()

    asyncio.run(run())
