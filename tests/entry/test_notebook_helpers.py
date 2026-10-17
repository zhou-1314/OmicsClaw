"""Registered helper sources survive mutable files and inject without history."""

import asyncio
import json
import sys

import pytest

from omicsclaw.entry.notebook.binding import SSHNotebookBinding
from omicsclaw.entry.notebook.helpers import bootstrap
from omicsclaw.entry.notebook.runtime import OwnerRuntime
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.schema import ToolCall
from omicsclaw.tools.context import use_tool_context
from tests.entry.test_notebook_owner_context import fresh_runtime, register
from tests.entry.test_ssh_notebook_binding import Client, registry

SOURCE = '__all__ = ["helper"]\ndef helper(value):\n    """Add a fixed amount."""\n    return value + 7\n'


def document():
    return {
        "sessionId": "chat",
        "skill": "fixture",
        "source": SOURCE,
        "body": "fixed instructions",
        "kernelPath": "/missing/fixture/kernel.py",
        "bodyPath": "/missing/fixture/SKILL.md",
        "componentDigest": "fixture-component",
    }


def test_unbound_helpers_persist_idempotently_and_invalidate_old_model_request(
    tmp_path,
):
    async def run():
        r = await fresh_runtime(tmp_path)
        try:
            await register(r, "/fixture/python")
            initial = await r.context({"sessionId": "chat", "requestId": "request"})
            first = await r.register_helper(document())
            second = await r.register_helper(document())
            assert first == second and first["helperRevision"] == 1
            assert not r.views.listing()["items"] and not r.manager.codes
            with pytest.raises(NotebookError, match="notebook_request_stale"):
                await r.check_request(
                    {
                        "sessionId": "chat",
                        "requestId": "request",
                        "token": initial["token"],
                    }
                )
            with pytest.raises(NotebookError, match="notebook_helper_invalid"):
                await r.register_helper(
                    {**document(), "source": SOURCE + "print('must not run')\n"}
                )
        finally:
            await r.close()
        restored = await fresh_runtime(tmp_path)
        try:
            context = await restored.context(
                {"sessionId": "chat", "requestId": "reconnected"}
            )
            assert context["summary"]["state"]["helperRevision"] == 1
            assert (await restored.register_helper(document()))["helperRevision"] == 1
        finally:
            await restored.close()

    asyncio.run(run())


def test_bootstrap_keeps_source_loader_fixed_and_refuses_namespace_collision():
    entry = {
        "skill": "fixture",
        "sourceHash": "a",
        "source": SOURCE,
        "kernelPath": "/no/file",
    }
    namespace = {}
    exec(bootstrap([entry]), namespace)
    assert namespace["helper"](2) == namespace["load_skill"]("fixture").helper(2) == 9
    assert namespace["fixture__helper"] is namespace["helper"]
    original = namespace["helper"]
    exec(bootstrap([entry]), namespace)
    assert namespace["helper"] is original
    conflict = {"helper": "user variable"}
    with pytest.raises(RuntimeError, match="conflicts"):
        exec(bootstrap([entry]), conflict)
    assert conflict["helper"] == "user variable" and "fixture__helper" not in conflict


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_owner_kernel_injects_fixed_source_before_approved_cell(tmp_path):
    pytest.importorskip("jupyter_client")
    pytest.importorskip("ipykernel")

    async def run():
        r = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="helper-component",
            env={},
        )
        await r.start()
        try:
            await register(r, sys.executable)
            await r.register_helper(document())
            binding = SSHNotebookBinding(Client(r))
            tools = registry(binding)

            async def approve(request):
                return True

            call = ToolCall(
                id="",
                name="python",
                arguments=json.dumps(
                    {"code": "print(helper(3), load_skill('fixture').helper(4))"}
                ),
            )
            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                prepared = await binding.notebook_augmentor("chat").prepare_request(
                    (), (), "request"
                )
                with tools.use_request_context(prepared.context, 0, call):
                    result = await tools.execute(call)
            assert not result.is_error, result.output
            result = json.loads(result.output)
            assert result["status"] == "completed", result
            assert result["output"]["stdout"] == "10 11\n"
            assert (
                r.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_executions"
                    ).fetchone()
                )[0]
                == 1
            )
            assert (
                r.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_helper_injections"
                    ).fetchone()
                )[0]
                == 1
            )
            assert (
                r.db.run(
                    lambda c: c.execute(
                        "SELECT source_digest FROM notebook_component_provenance WHERE kind='execution'"
                    ).fetchone()
                )[0]
                == "helper-component"
            )
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_use_skill_invalidates_same_completion_then_refreshes_without_user_resume(
    tmp_path,
):
    from omicsclaw.engine import AgentEngine, EngineConfig
    from omicsclaw.permission import PermissionGate, gate_tools
    from omicsclaw.schema import Message, Role
    from omicsclaw.skills import load_skills, use_skill_tool
    from omicsclaw.tools.builtin.python_kernel import PythonKernelTool
    from omicsclaw.tools.registry import ToolRegistry
    from tests.engine.test_augmentor import Provider
    from tests.entry.test_ssh_notebook_binding import action

    async def run():
        directory = tmp_path / "skills" / "fixture"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            "---\nname: fixture\ndescription: test helper\n---\nFixed instructions.\n"
        )
        (directory / "kernel.py").write_text(SOURCE)
        r = await fresh_runtime(tmp_path)
        try:
            await register(r, "/fixture/python")
            binding = SSHNotebookBinding(Client(r))
            index = load_skills(directory.parent)
            tools = ToolRegistry(
                gate_tools(
                    (
                        use_skill_tool(index, annotate=binding.annotate_skill),
                        PythonKernelTool(runner=binding),
                    ),
                    PermissionGate(),
                ),
                before_execute=binding.before_tool,
            )
            provider = Provider(
                Message(
                    role=Role.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="register",
                            name="use_skill",
                            arguments='{"skill_name":"fixture"}',
                        ),
                        action("stale = helper(1)"),
                    ),
                ),
                Message(role=Role.ASSISTANT, tool_calls=(action("fresh = helper(2)"),)),
                Message(role=Role.ASSISTANT, content="done"),
            )
            approvals = []

            async def approve(request):
                approvals.append(request)
                return True

            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                result = await AgentEngine(provider, tools, EngineConfig()).run(
                    (Message(role=Role.USER, content="use fixture"),),
                    augmentor=binding.notebook_augmentor("chat"),
                )
            assert r.manager.codes == ["fresh = helper(2)"] and len(approvals) == 1
            assert any(
                m.is_error and "notebook_request_stale" in m.content
                for m in result.messages
            )
            bundle = json.loads(
                r.db.run(
                    lambda c: c.execute(
                        "SELECT bundle_json FROM notebook_execution_helpers"
                    ).fetchone()
                )[0]
            )
            assert bundle[0]["body"] == "Fixed instructions."
            assert bundle[0]["registration"]["toolCallId"] == "register"
            assert bundle[0]["exports"][0][0] == "helper"
            await r.register_helper({**document(), "body": "later instructions"})
            retained = json.loads(
                r.db.run(
                    lambda c: c.execute(
                        "SELECT bundle_json FROM notebook_execution_helpers"
                    ).fetchone()
                )[0]
            )
            assert retained == bundle
        finally:
            await r.close()

    asyncio.run(run())


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_helper_collision_manual_repair_and_new_generation(tmp_path):
    pytest.importorskip("ipykernel")

    async def run():
        r = OwnerRuntime(
            tmp_path,
            {"authorityId": "a" * 32, "workspaceId": "b" * 32},
            source_digest="fixed",
            env={},
        )
        await r.start()
        try:
            environment = await register(r, sys.executable)
            binding = SSHNotebookBinding(Client(r))
            tools = registry(binding)

            async def approve(request):
                return True

            async def agent(code, request):
                call = ToolCall(
                    id=request, name="python", arguments=json.dumps({"code": code})
                )
                with use_tool_context(approval=approve, values={"session_id": "chat"}):
                    prepared = await binding.notebook_augmentor("chat").prepare_request(
                        (), (), request
                    )
                    with tools.use_request_context(prepared.context, 0, call):
                        response = await tools.execute(call)
                assert not response.is_error, response.output
                return json.loads(response.output)

            first = await agent("helper = 'user value'", "first")
            nid = first["notebook_id"]
            await r.register_helper(document())
            failed = await agent("dependent_must_not_run = True", "blocked")
            assert failed["status"] == "failed"
            assert (
                r.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_helper_injections"
                    ).fetchone()
                )[0]
                == 0
            )
            kernel = r.views.snapshot(nid)["kernel"]
            command = {
                "version": 1,
                "notebookId": nid,
                "cellId": "repair",
                "commandId": "repair",
                "source": "assert helper == 'user value'\nassert 'dependent_must_not_run' not in globals()\ndel helper",
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
            receipt = await r.admit({"command": command})
            for _ in range(300):
                row = r.views.execution(nid, receipt["execution_id"])
                if row["durability"] != "pending":
                    break
                await asyncio.sleep(0.01)
            assert row["status"] == "completed"
            await r.resume(
                {
                    "notebookId": nid,
                    "commandId": "resume",
                    "userWatermark": 1,
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": 1,
                }
            )
            retried = await agent("print(helper(2))", "retry")
            assert (
                retried["status"] == "completed"
                and retried["output"]["stdout"] == "9\n"
            )
            await r.release(
                {
                    "notebookId": nid,
                    "commandId": "release",
                    "kernelInstanceId": kernel["kernelInstanceId"],
                    "generation": 1,
                }
            )
            bound = await r.bind(
                {
                    "notebookId": nid,
                    "sessionId": "chat",
                    "commandId": "rebind",
                    "environmentId": environment["environmentId"],
                    "environmentRevision": environment["revision"],
                }
            )
            assert bound["kernel"]["generation"] == 2
            restarted = await agent("print(helper(3))", "new-generation")
            assert restarted["output"]["stdout"] == "10\n"
            assert (
                r.db.run(
                    lambda c: c.execute(
                        "SELECT count(*) FROM notebook_helper_injections"
                    ).fetchone()
                )[0]
                == 2
            )
        finally:
            await r.close()

    asyncio.run(run())
