"""Scripted models use the owner through the same runner and approval seams as SSH."""

import asyncio
import json

import pytest

from omicsclaw.engine import AgentEngine, EngineConfig
from omicsclaw.entry.notebook.binding import SSHNotebookBinding
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.permission import PermissionGate, gate_tools
from omicsclaw.schema import Message, Role, ToolCall
from omicsclaw.tools.builtin.python_kernel import PythonKernelTool
from omicsclaw.tools.context import use_tool_context
from omicsclaw.tools.registry import ToolRegistry
from tests.engine.test_augmentor import Provider
from tests.entry.test_notebook_owner import setup
from tests.entry.test_notebook_owner_context import fresh_runtime, register

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


class Client:
    def __init__(self, runtime):
        self.runtime = runtime

    async def attach(self):
        return self.runtime.health()

    async def call(self, method, params, *, expected_owner):
        if expected_owner != {
            "ownerId": self.runtime.owner_id,
            "ownerEpoch": self.runtime.epoch,
        }:
            raise NotebookError("notebook_owner_changed", 409)
        if method == "execution":
            return self.runtime.views.execution(
                params["notebookId"], params["executionId"]
            )
        return await self.runtime.mutate(method, params)


def registry(binding):
    return ToolRegistry(
        gate_tools((PythonKernelTool(runner=binding),), PermissionGate()),
        before_execute=binding.before_tool,
    )


def action(code, call_id=""):
    return ToolCall(
        id=call_id,
        name="python",
        arguments=json.dumps({"code": code, "timeout_secs": 120}),
    )


def test_first_approved_agent_cell_binds_once_and_next_model_request_sees_state(
    tmp_path,
):
    async def run():
        runtime = await fresh_runtime(tmp_path)
        try:
            await register(runtime, "/fixture/python")
            binding = SSHNotebookBinding(Client(runtime))
            provider = Provider(
                Message(
                    role=Role.ASSISTANT,
                    tool_calls=(action("x = 1"), action("x += 100")),
                ),
                Message(role=Role.ASSISTANT, tool_calls=(action("x += 2"),)),
                Message(role=Role.ASSISTANT, content="done"),
            )
            prompts = []

            async def approve(request):
                prompts.append(request)
                return True

            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                result = await AgentEngine(
                    provider, registry(binding), EngineConfig()
                ).run(
                    (Message(role=Role.USER, content="analyze"),),
                    augmentor=binding.notebook_augmentor("chat"),
                )
            assert runtime.manager.codes == ["x = 1", "x += 2"]
            assert len(prompts) == 2  # The stale same-completion call never asks.
            assert len(runtime.views.listing()["items"]) == 1
            assert any(
                message.is_error and "notebook_request_stale" in message.content
                for message in result.messages
            )
            assert '"stateSeq":1' in provider.seen[1][-1].content
            assert not any(
                '"token"' in message.content
                for sent in provider.seen
                for message in sent
            )
            record = runtime.views.snapshot(
                runtime.views.listing()["items"][0]["notebook_id"]
            )["latestExecution"]
            assert record["runtime"]["timeoutSeconds"] == 120
            assert "token" not in record["runtime"]["preconditions"]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_denied_first_cell_does_not_create_binding_or_kernel(tmp_path):
    async def run():
        runtime = await fresh_runtime(tmp_path)
        try:
            await register(runtime, "/fixture/python")
            binding = SSHNotebookBinding(Client(runtime))

            async def deny(request):
                return False

            call = action("x = 1")
            with use_tool_context(approval=deny, values={"session_id": "chat"}):
                prepared = await binding.notebook_augmentor("chat").prepare_request(
                    (), (), "request"
                )
                tools = registry(binding)
                with tools.use_request_context(prepared.context, 0, call):
                    result = await tools.execute(call)
            assert result.is_error
            assert not runtime.manager.codes and not runtime.views.listing()["items"]
        finally:
            await runtime.close()

    asyncio.run(run())


def test_user_takeover_during_approval_refuses_late_allow(tmp_path, monkeypatch):
    async def run():
        runtime, manager, user_command = await setup(tmp_path, monkeypatch)
        try:
            binding = SSHNotebookBinding(Client(runtime))
            asked, released = asyncio.Event(), asyncio.Event()

            async def approve(request):
                asked.set()
                await released.wait()
                return True

            call = action("must_not_run = 1")
            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                prepared = await binding.notebook_augmentor("chat").prepare_request(
                    (), (), "request"
                )
                tools = registry(binding)
                with tools.use_request_context(prepared.context, 0, call):
                    pending = asyncio.create_task(tools.execute(call))
                async with asyncio.timeout(5):
                    while not asked.is_set():
                        assert not pending.done(), pending.result()
                        await asyncio.sleep(0)
                await runtime.admit({"command": user_command})
                released.set()
                result = await pending
            assert result.is_error and "notebook_user_control" in result.output
            assert "must_not_run = 1" not in manager.codes
        finally:
            await runtime.close()

    asyncio.run(run())


def test_subagent_prepares_its_own_request_and_cannot_borrow_parent_token(tmp_path):
    from omicsclaw.entry.config import AppConfig
    from omicsclaw.entry.subagent import GENERAL_PURPOSE, ChildRunner
    from omicsclaw.tools.context import current_context

    async def run():
        runtime = await fresh_runtime(tmp_path)
        try:
            await register(runtime, "/fixture/python")
            binding = SSHNotebookBinding(Client(runtime))
            tools = registry(binding)
            provider = Provider(
                Message(role=Role.ASSISTANT, tool_calls=(action("child_value = 1"),)),
                Message(role=Role.ASSISTANT, content="child finished"),
            )
            runner = ChildRunner(
                provider=provider,
                parent=tools,
                config=AppConfig(workspace=tmp_path),
                notebook_augmentor=binding.notebook_augmentor,
            )
            prompts = []

            async def approve(request):
                prompts.append(request)
                return True

            with use_tool_context(approval=approve, values={"session_id": "parent"}):
                parent = await binding.notebook_augmentor("parent").prepare_request(
                    (), (), "parent-request"
                )
                with tools.use_request_context(
                    parent.context, 0, action("parent_value = 1")
                ):
                    original = current_context().invocation
                    assert (
                        await runner.delegate(GENERAL_PURPOSE, "run a child cell")
                        == "child finished"
                    )
                    assert current_context().invocation is original
            bindings = runtime.views.listing()["items"]
            assert len(bindings) == 1 and bindings[0]["binding"][
                "session_id"
            ].startswith("child-")
            assert bindings[0]["binding"]["session_id"] != "parent"
            assert runtime.manager.codes == ["child_value = 1"] and len(prompts) == 1
            # A missing private invocation is rejected before even asking.
            with use_tool_context(
                approval=approve, values={"session_id": "child-forged"}
            ):
                response = await runner._child_registry(GENERAL_PURPOSE).execute(
                    action("forbidden = 1")
                )
            assert response.is_error and "notebook_request_invalid" in response.output
            assert len(prompts) == 1
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("cancel_observer", [False, True])
def test_observer_budget_or_cancellation_never_cancels_accepted_science(
    tmp_path, monkeypatch, cancel_observer
):
    from types import SimpleNamespace

    async def run():
        runtime = await fresh_runtime(tmp_path)
        waiting = None
        try:
            await register(runtime, "/fixture/python")
            runtime.manager.wait = asyncio.Event()
            binding = SSHNotebookBinding(Client(runtime))
            tools = registry(binding)
            call = action("held_science = 1")

            async def approve(request):
                return True

            if not cancel_observer:
                ticks = iter([0, 16])
                monkeypatch.setattr(
                    "omicsclaw.entry.notebook.binding.time",
                    SimpleNamespace(monotonic=lambda: next(ticks)),
                )
            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                prepared = await binding.notebook_augmentor("chat").prepare_request(
                    (), (), "request"
                )
                with tools.use_request_context(prepared.context, 0, call):
                    task = asyncio.create_task(tools.execute(call))
                async with asyncio.timeout(5):
                    while not runtime.manager.codes:
                        if task.done():
                            finished = task.result()
                            assert not finished.is_error, finished.output
                        await asyncio.sleep(0)
                if cancel_observer:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = json.loads((await task).output)
                    assert result["stillRunning"] and result["status"] in {
                        "queued",
                        "running",
                    }
                    assert "do not resubmit" in result["notice"]
            waiting = asyncio.create_task(
                binding.notebook_augmentor("chat").prepare_request((), (), "next")
            )
            async with asyncio.timeout(5):
                while not binding.waiters:
                    assert not waiting.done(), waiting.result()
                    await asyncio.sleep(0)
            assert not waiting.done()
            runtime.manager.wait.set()
            await asyncio.wait_for(waiting, 2)
            nid = runtime.views.listing()["items"][0]["notebook_id"]
            latest = runtime.views.snapshot(nid)["latestExecution"]
            assert (
                latest["status"] == "completed"
                and latest["runtime"]["timeoutSeconds"] == 120
            )
            assert runtime.manager.codes == ["held_science = 1"]
        finally:
            if waiting and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await runtime.close()

    asyncio.run(run())
