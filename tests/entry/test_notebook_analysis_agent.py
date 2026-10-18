"""Agent metadata writes retain trusted requests and recover only owned receipts."""

import asyncio
import json

import pytest

from omicsclaw.engine import AgentEngine, EngineConfig
from omicsclaw.entry.notebook.binding import SSHNotebookBinding
from omicsclaw.entry.notebook.service import NotebookError
from omicsclaw.permission import PermissionGate, gate_tools
from omicsclaw.schema import Message, Role, ToolCall
from omicsclaw.tools.builtin.notebook_analysis import NotebookAnalysisTool
from omicsclaw.tools.builtin.python_kernel import PythonKernelTool
from omicsclaw.tools.context import use_tool_context
from omicsclaw.tools.registry import ToolRegistry
from tests.engine.test_augmentor import Provider
from tests.entry.test_notebook_analysis_owner import registration, settled
from tests.entry.test_notebook_r import bind, create, environment
from tests.entry.test_ssh_notebook_binding import Client

pytestmark = pytest.mark.usefixtures("notebook_control_rss")


class LostAck(Client):
    def __init__(self, runtime, error_type=TimeoutError, action="object.register"):
        super().__init__(runtime)
        self.error_type = error_type
        self.action = action
        self.accepted = None
        self.observations = []

    async def call(self, method, params, **kwargs):
        result = await super().call(method, params, **kwargs)
        if (
            method == "analysis_agent"
            and params["action"] == self.action
            and self.accepted is None
        ):
            self.accepted = params
            raise self.error_type("notebook_owner_unavailable")
        if method == "analysis_agent" and params["action"] == "receipt":
            self.observations.append(result)
        return result


@pytest.mark.parametrize("error_type", [TimeoutError, NotebookError])
def test_real_agent_registration_lost_ack_stale_same_completion_and_owned_lookup(
    tmp_path,
    error_type,
):
    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, "python", real=True)
            kernel = (await bind(owner, "python", env))["kernel"]
            args = registration(kernel)["arguments"]
            args.update(
                adapter="python_table",
                rowIdentity={"kind": "index"},
                columnIdentity={"kind": "index"},
                declaredMatrix={"assay": None, "layer": None, "semantics": "unknown"},
            )
            client = LostAck(owner, error_type)
            binding = SSHNotebookBinding(client)
            tools = ToolRegistry(
                gate_tools(
                    (PythonKernelTool(runner=binding), NotebookAnalysisTool(binding)),
                    PermissionGate(),
                ),
                before_execute=binding.before_tool,
            )
            provider = Provider(
                Message(
                    role=Role.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="create",
                            name="python",
                            arguments=json.dumps(
                                {
                                    "code": "import pandas as pd\ndata = pd.DataFrame({'count':[1,2]}, index=['a','b'])"
                                }
                            ),
                        ),
                    ),
                ),
                Message(
                    role=Role.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="register",
                            name="notebook_analysis",
                            arguments=json.dumps(
                                {"action": "object.register", "arguments": args}
                            ),
                        ),
                        ToolCall(
                            id="stale",
                            name="python",
                            arguments=json.dumps({"code": "must_not_execute = True"}),
                        ),
                    ),
                ),
                Message(role=Role.ASSISTANT, content="done"),
            )
            approvals = []

            async def approve(request):
                approvals.append(request)
                return True

            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                result = await AgentEngine(provider, tools, EngineConfig()).run(
                    (Message(role=Role.USER, content="register the analysis table"),),
                    augmentor=binding.notebook_augmentor("chat"),
                )
            assert client.accepted is not None, result
            assert client.observations[0]["found"] is True
            eid = client.observations[0]["receipt"]["result"]["operationId"]
            assert (await settled(owner, eid))["status"] == "completed"
            assert len(approvals) == 2
            assert any(
                message.is_error and "notebook_request_stale" in message.content
                for message in result.messages
            )
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT COUNT(*) FROM notebook_analysis_operations"
                    ).fetchone()[0]
                )
                == 1
            )
            assert not any(
                '"token"' in msg.content for sent in provider.seen for msg in sent
            )
            stale = client.accepted
            query = {
                **stale,
                "action": "receipt",
                "arguments": {"commandId": stale["commandId"]},
            }
            assert (await owner.analysis_agent(query))["found"]
            with pytest.raises(NotebookError, match="command_conflict"):
                await owner.analysis_agent(
                    {**stale, "arguments": {**args, "symbol": "other"}}
                )
            fresh = await owner.context({"sessionId": "chat", "requestId": "fresh"})
            missing = await owner.analysis_agent(
                {
                    **query,
                    "requestId": "fresh",
                    "token": fresh["token"],
                    "arguments": {"commandId": "never-accepted"},
                }
            )
            assert missing == {"found": False, "receipt": None}
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT COUNT(*) FROM notebook_analysis_operations"
                    ).fetchone()[0]
                )
                == 1
            )
            # A different, authenticated session cannot read this receipt.
            await owner.bind(
                {
                    "notebookId": "other-nb",
                    "sessionId": "other",
                    "commandId": "other-bind",
                    "language": "python",
                    "environmentId": env["environmentId"],
                    "environmentRevision": env["revision"],
                }
            )
            other = await owner.context(
                {"sessionId": "other", "requestId": "other-request"}
            )
            assert await owner.analysis_agent(
                {
                    **query,
                    "sessionId": "other",
                    "requestId": "other-request",
                    "token": other["token"],
                }
            ) == {"found": False, "receipt": None}
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("takeover_at", ["inference", "approval"])
def test_metadata_request_is_checked_before_and_after_permission(tmp_path, takeover_at):
    async def run():
        owner = await create(tmp_path)
        try:
            env = await environment(owner, "python")
            kernel = (await bind(owner, "python", env))["kernel"]
            binding = SSHNotebookBinding(Client(owner))
            tools = ToolRegistry(
                gate_tools((NotebookAnalysisTool(binding),), PermissionGate()),
                before_execute=binding.before_tool,
            )
            call = ToolCall(
                id="register",
                name="notebook_analysis",
                arguments=json.dumps(
                    {
                        "action": "object.register",
                        "arguments": registration(kernel)["arguments"],
                    }
                ),
            )

            class ChangingProvider(Provider):
                async def generate(self, messages, tools=None):
                    result = await super().generate(messages, tools)
                    if takeover_at == "inference" and len(self.seen) == 1:
                        owner.service._transaction(
                            lambda c: owner.requests.takeover(c, "nb")
                        )
                    return result

            provider = ChangingProvider(
                Message(role=Role.ASSISTANT, tool_calls=(call,)),
                Message(role=Role.ASSISTANT, content="stopped"),
            )
            approvals = []

            async def approve(request):
                approvals.append(request)
                owner.service._transaction(lambda c: owner.requests.takeover(c, "nb"))
                return True

            # Avoid a second request waiting for an intentional user takeover.
            class Once:
                async def prepare_request(self, history, definitions, request_id):
                    if provider.seen:
                        owner.service._transaction(
                            lambda c: c.execute(
                                "UPDATE notebook_request_control SET control_mode='agent' WHERE notebook_id='nb'"
                            )
                        )
                    return await binding.notebook_augmentor("chat").prepare_request(
                        history, definitions, request_id
                    )

            with use_tool_context(approval=approve, values={"session_id": "chat"}):
                result = await AgentEngine(provider, tools, EngineConfig()).run(
                    (Message(role=Role.USER, content="inspect"),), augmentor=Once()
                )
            assert len(approvals) == (0 if takeover_at == "inference" else 1)
            assert any(
                message.is_error and "notebook_request_stale" in message.content
                for message in result.messages
            )
            assert not owner.manager.codes
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT COUNT(*) FROM notebook_analysis_operations"
                    ).fetchone()[0]
                )
                == 0
            )
        finally:
            await owner.close()

    asyncio.run(run())


def test_remove_keeps_permission_gate_and_lost_receipt_does_not_repeat_mutation(
    tmp_path,
):
    from tests.entry.test_notebook_analysis_owner import reference

    async def run():
        owner = await create(tmp_path)
        try:
            env = await environment(owner, "python")
            kernel = (await bind(owner, "python", env))["kernel"]

            def seed(c):
                owner.analysis.store.put(
                    c,
                    "nb",
                    "object",
                    {
                        "objectId": "object",
                        "objectRevision": 1,
                        "kernel": reference(kernel),
                        "symbol": "data",
                        "availability": "needs_inspection",
                        "reason": "bad_identity",
                        "defaultView": None,
                    },
                )
                owner.analysis.store.advance(c, "nb")

            owner.service._transaction(seed)
            client = LostAck(owner, action="object.remove")
            binding = SSHNotebookBinding(client)
            tools = ToolRegistry(
                gate_tools((NotebookAnalysisTool(binding),), PermissionGate()),
                before_execute=binding.before_tool,
            )
            approvals = []
            for allow in (False, True):
                provider = Provider(
                    Message(
                        role=Role.ASSISTANT,
                        tool_calls=(
                            ToolCall(
                                id="remove",
                                name="notebook_analysis",
                                arguments=json.dumps(
                                    {
                                        "action": "object.remove",
                                        "arguments": {
                                            "object": {
                                                "objectId": "object",
                                                "objectRevision": 1,
                                            }
                                        },
                                    }
                                ),
                            ),
                        ),
                    ),
                    Message(role=Role.ASSISTANT, content="done"),
                )

                async def approve(request, allowed=allow):
                    approvals.append(request)
                    return allowed

                with use_tool_context(approval=approve, values={"session_id": "chat"}):
                    await AgentEngine(provider, tools, EngineConfig()).run(
                        (
                            Message(
                                role=Role.USER,
                                content="Cancel this registration, keeping data in memory",
                            ),
                        ),
                        augmentor=binding.notebook_augmentor("chat"),
                    )
                obj = owner.analysis.store.state_response(
                    {"notebookId": "nb", "subject": {"kind": "object", "id": "object"}}
                )["item"]
                assert obj["registrationState"] == ("removed" if allow else "active")
            assert len(approvals) == 2 and len(client.observations) == 1
            receipt = client.observations[0]
            assert (
                receipt["found"] and receipt["receipt"]["result"]["operationId"] is None
            )
            assert not owner.manager.codes
            assert (
                owner.analysis.store.state_response({"notebookId": "nb"})[
                    "analysisRevision"
                ]
                == 2
            )
            observed = await owner.analysis_agent(client.accepted)
            assert observed["found"] and observed["receipt"] == receipt["receipt"]
            assert (
                owner.analysis.store.state_response({"notebookId": "nb"})[
                    "analysisRevision"
                ]
                == 2
            )
        finally:
            await owner.close()

    asyncio.run(run())
