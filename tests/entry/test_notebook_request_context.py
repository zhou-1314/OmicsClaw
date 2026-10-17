"""Model request state is private, immutable and scoped before the permission gate."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from omicsclaw.engine import AgentEngine, EngineConfig
from omicsclaw.permission import PermissionGate, gate_tools
from omicsclaw.provider import ProviderError
from omicsclaw.schema import Message, Role, ToolCall, ToolDefinition
from omicsclaw.schema.request import ModelRequestContext, PreparedModelRequest
from omicsclaw.tools.base import ApprovalMode, ToolPolicy
from omicsclaw.tools.context import current_context, require_approval, use_tool_context
from omicsclaw.tools.registry import ToolRegistry
from tests.engine.test_augmentor import Provider


class Tool:
    name = "python"
    policy = ToolPolicy(approval_mode=ApprovalMode.AUTO)

    def __init__(self, calls):
        self.calls = calls

    def definition(self):
        return ToolDefinition(name=self.name, description="fixture")

    async def execute(self, arguments):
        self.calls.append(current_context().invocation)
        await asyncio.sleep(0)
        return "done"


class Preparation:
    def __init__(self, label):
        self.label, self.requests = label, []

    async def prepare_request(self, history, tools, request_id):
        self.requests.append(request_id)
        return PreparedModelRequest(
            (Message(role=Role.USER, content="untrusted summary " + self.label),),
            ModelRequestContext(request_id, (("fixture", self.label),)),
        )


def test_request_state_is_copied_and_immutable():
    values = [["scope", "original"]]
    context = ModelRequestContext("request", values)
    values[0][1] = "changed"
    assert context.value("scope") == "original"
    with pytest.raises(FrozenInstanceError):
        context.request_id = "changed"


def test_parallel_requests_duplicate_tool_ids_and_provider_retries_keep_their_state():
    async def run(label):
        calls, preparation = [], Preparation(label)
        action = Message(
            role=Role.ASSISTANT,
            tool_calls=(
                ToolCall(id="", name="python", arguments='{"code":"a"}'),
                ToolCall(id="", name="python", arguments='{"code":"b"}'),
            ),
        )

        class Retry(Provider):
            failed = False

            async def generate(self, messages, tools=None):
                if not self.failed:
                    self.failed = True
                    self.first = tuple(messages)
                    raise ProviderError(
                        "retry fixture", provider="fixture", status_code=503
                    )
                result = await super().generate(messages, tools)
                if len(self.seen) == 1:
                    assert self.seen[0] == self.first
                return result

        provider = Retry(action, Message(role=Role.ASSISTANT, content="done"))
        registry = ToolRegistry((Tool(calls),))
        with use_tool_context(values={"fixture": "forged", "request_id": "forged"}):
            result = await AgentEngine(
                provider, registry, EngineConfig(generate_retry_base=0)
            ).run((Message(role=Role.USER, content="go"),), augmentor=preparation)
            assert current_context().invocation is None
        assert len(preparation.requests) == 2
        assert len(set(preparation.requests)) == 2
        assert [call.position for call in calls] == [0, 1]
        assert {call.request.request_id for call in calls} == {preparation.requests[0]}
        assert {call.request.value("fixture") for call in calls} == {label}
        assert [call.call.arguments for call in calls] == [
            '{"code":"a"}',
            '{"code":"b"}',
        ]
        assert not any(
            "untrusted summary" in str(message.content) for message in result.messages
        )
        return preparation.requests[0]

    async def both():
        ids = await asyncio.gather(run("one"), run("two"))
        assert ids[0] != ids[1]

    asyncio.run(both())


@pytest.mark.parametrize("self_asks", [False, True])
def test_freshness_precheck_precedes_both_permission_prompt_paths_and_restores_scope(
    self_asks,
):
    async def run():
        calls, prompts = [], []
        stale = True

        class Asking(Tool):
            policy = ToolPolicy(
                approval_mode=ApprovalMode.ASK, prompts_for_itself=self_asks
            )

            async def execute(self, arguments):
                if self_asks:
                    await require_approval(self.name, arguments, policy=self.policy)
                return await super().execute(arguments)

        async def check(call):
            assert current_context().invocation.request.value("fixture") == "current"
            if stale:
                raise ValueError("stale_state")

        async def approve(request):
            prompts.append(request)
            return True

        registry = ToolRegistry(
            gate_tools((Asking(calls),), PermissionGate()), before_execute=check
        )
        call = ToolCall(id="call", name="python", arguments="{}")
        context = ModelRequestContext("request", (("fixture", "current"),))
        with use_tool_context(approval=approve):
            with registry.use_request_context(context, 0, call):
                result = await registry.execute(call)
            assert result.is_error and "stale_state" in result.output
            assert not calls and not prompts
            assert current_context().invocation is None
            stale = False
            with registry.use_request_context(context, 1, call):
                result = await registry.execute(call)
            assert not result.is_error
            assert len(calls) == len(prompts) == 1
            assert current_context().invocation is None

    asyncio.run(run())
