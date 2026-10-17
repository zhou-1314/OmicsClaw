"""Agent-facing owner proxy; observation never owns a scientific execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from omicsclaw.schema import Message, Role
from omicsclaw.schema.request import ModelRequestContext, PreparedModelRequest
from omicsclaw.tools.context import context_value, current_context

from ..notebook_context import MAX_CONTEXT_BYTES, NOTICE
from .contracts import MAX_TIMEOUT_SECONDS, encoded
from .service import NotebookError


class SSHNotebookBinding:
    execution_timeout = MAX_TIMEOUT_SECONDS
    default_execution_timeout = 1800.0

    def __init__(self, client):
        self.client = client
        self.waiters = {}

    async def call(self, method, params, *, owner=None):
        if owner is None:
            health = await self.client.attach()
            owner = {k: health["owner"][k] for k in ("ownerId", "ownerEpoch")}
        return await self.client.call(method, params, expected_owner=owner)

    def notebook_augmentor(self, session_id):
        return SSHNotebookContext(self, session_id)

    def invocation(self):
        invocation = current_context().invocation
        value = invocation.request.value("notebook") if invocation else None
        if value is None:
            raise NotebookError("notebook_request_invalid", 409)
        state = json.loads(value)
        sid = str(context_value("session_id", ""))
        if state["sessionId"] != sid:
            raise NotebookError("notebook_request_invalid", 409)
        return invocation, state

    async def before_tool(self, call):
        if call.name not in {"python", "r", "notebook_analysis"}:
            return
        invocation, state = self.invocation()
        if invocation.call != call:
            raise NotebookError("notebook_request_invalid", 409)
        if (
            call.name == "notebook_analysis"
            and json.loads(call.arguments).get("action") == "receipt"
        ):
            # The owner still authenticates the session and exact receipt ownership.
            return
        await self.call(
            "check_analysis_request"
            if call.name == "notebook_analysis"
            else "check_request",
            {
                "sessionId": state["sessionId"],
                "requestId": invocation.request.request_id,
                "token": state["token"],
                **(
                    {"action": json.loads(call.arguments).get("action")}
                    if call.name == "notebook_analysis"
                    else {"language": call.name}
                ),
            },
            owner=state["owner"],
        )

    async def run_analysis(self, params):
        invocation, state = self.invocation()
        if (
            invocation.call.name != "notebook_analysis"
            or json.loads(invocation.call.arguments) != params
        ):
            raise NotebookError("notebook_request_invalid", 409)
        await self.before_tool(invocation.call)
        payload = {
            **params,
            "sessionId": state["sessionId"],
            "requestId": invocation.request.request_id,
            "token": state["token"],
            "toolPosition": invocation.position,
            "commandId": hashlib.sha256(
                encoded(
                    [
                        "analysis",
                        state["sessionId"],
                        invocation.request.request_id,
                        invocation.position,
                    ]
                )
            ).hexdigest(),
        }
        try:
            result = await self.call("analysis_agent", payload, owner=state["owner"])
        except (TimeoutError, ConnectionError, OSError, NotebookError) as error:
            if isinstance(error, NotebookError) and error.code not in {
                "notebook_owner_unavailable",
                "notebook_owner_changed",
            }:
                raise
            try:
                result = await self.call(
                    "analysis_agent",
                    {
                        **payload,
                        "action": "receipt",
                        "arguments": {"commandId": payload["commandId"]},
                    },
                    owner=state["owner"],
                )
            except (TimeoutError, ConnectionError, OSError, NotebookError):
                result = {"found": None, "receipt": None}
            result.update(
                commandId=payload["commandId"],
                notice="Only query this receipt. Do not recreate this action with a new command ID.",
            )
        receipt = result.get("receipt") or {}
        eid = receipt.get("result", {}).get("operationId")
        if eid and receipt.get("result", {}).get("entityKind") == "checkpoint":
            nid = state["summary"]["binding"]["notebook_id"]
            try:
                result["operation"] = (
                    await self.call(
                        "analysis_state",
                        {
                            "notebookId": nid,
                            "subject": {"kind": "checkpointOperation", "id": eid},
                        },
                        owner=state["owner"],
                    )
                )["item"]
            except (TimeoutError, ConnectionError, OSError, NotebookError):
                result["notice"] = (
                    "Checkpoint accepted; observe this operation or receipt without recreating it."
                )
        elif eid:
            nid = state["summary"]["binding"]["notebook_id"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    record = await self.call(
                        "execution",
                        {"notebookId": nid, "executionId": eid},
                        owner=state["owner"],
                    )
                except (TimeoutError, ConnectionError, OSError, NotebookError):
                    result.update(
                        commandId=payload["commandId"],
                        notice="The operation was accepted. Query this receipt; do not repeat the action.",
                    )
                    break
                result["operation"] = {
                    key: record[key] for key in ("execution_id", "status", "durability")
                }
                if (
                    record["status"] not in {"queued", "running"}
                    and record["durability"] != "pending"
                ):
                    break
                await asyncio.sleep(0.1)
        return encoded(result).decode()

    async def run_python(self, code, **kwargs):
        return await self._run_cell(code, language="python", **kwargs)

    async def run_r(self, code, **kwargs):
        return await self._run_cell(code, language="r", **kwargs)

    async def _run_cell(
        self,
        code,
        *,
        language,
        description,
        session_id,
        on_output=None,
        timeout_s=None,
        location="local",
    ):
        invocation, state = self.invocation()
        original = json.loads(invocation.call.arguments)
        requested = original.get("timeout_secs")
        expected_timeout = (
            min(max(float(requested), 1.0), self.execution_timeout)
            if isinstance(requested, (int, float))
            else None
        )
        if (
            invocation.call.name != language
            or location != "local"
            or original.get("code") != code
            or state["sessionId"] != session_id
            or expected_timeout != timeout_s
        ):
            raise NotebookError("notebook_request_invalid", 409)
        # This runs after approval; the owner repeats the check in its commit transaction.
        await self.before_tool(invocation.call)
        summary = state["summary"]
        binding, kernel, selection = (
            summary["binding"],
            summary["kernels"][language],
            summary["selections"][language],
        )
        nid = (
            binding["notebook_id"]
            if binding
            else hashlib.sha256(("notebook:" + session_id).encode()).hexdigest()
        )
        command_id = hashlib.sha256(
            encoded([session_id, invocation.request.request_id, invocation.position])
        ).hexdigest()
        command = {
            "version": 4 if "analysis" in summary else 2,
            "notebookId": nid,
            "cellId": command_id,
            "commandId": command_id,
            "source": code,
            "expectedRevision": 0,
            "language": language,
            "origin": "agent",
            "approvalScope": "permission_gate",
            "timeoutSeconds": timeout_s or self.default_execution_timeout,
            "kernelInstanceId": kernel["kernelInstanceId"]
            if kernel
            else hashlib.sha256(
                (
                    "kernel:"
                    + session_id
                    + ":"
                    + invocation.request.request_id
                    + (":r" if language == "r" else "")
                ).encode()
            ).hexdigest()[:32],
            "generation": kernel["generation"] if kernel else summary["nextGeneration"],
            "environmentId": selection["environmentId"],
            "environmentRevision": selection["environmentRevision"],
            "preconditions": {
                "sessionId": session_id,
                "requestId": invocation.request.request_id,
                "token": state["token"],
                "toolPosition": invocation.position,
                **(
                    {"analysis": summary["analysis"]["guards"][language]}
                    if "analysis" in summary
                    else {}
                ),
            },
        }
        record = await self.call(
            "admit_agent", {"command": command}, owner=state["owner"]
        )
        deadline = time.monotonic() + 15
        while (
            record["status"] in {"queued", "running"}
            or record["durability"] == "pending"
        ):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.15)
            record = await self.call(
                "execution",
                {"notebookId": nid, "executionId": record["execution_id"]},
                owner=state["owner"],
            )
        return json.dumps(
            {
                "notebook_id": nid,
                "cell_id": command_id,
                "execution_id": record["execution_id"],
                "status": record["status"],
                "durability": record["durability"],
                "output": record.get("output"),
                "stillRunning": record["status"] in {"queued", "running"},
                "notice": "Observe this execution; do not resubmit its code.",
            },
            ensure_ascii=False,
        )

    async def annotate_skill(self, skill, body):
        from omicsclaw.skills.sidecar import render_kernel_plugin_block

        from ..ssh.manifest import verified_source_metadata

        path = skill.directory / "kernel.py"
        if not path.is_file():
            return ""
        if path.stat().st_size > 16384:
            raise NotebookError("notebook_helper_invalid")
        root = Path(__file__).resolve().parents[3]
        metadata = (
            verified_source_metadata(root)
            if (root / "controller-manifest.json").exists()
            else {}
        )
        invocation = current_context().invocation
        registration = (
            {
                "kind": "tool",
                "requestId": invocation.request.request_id,
                "toolPosition": invocation.position,
                "toolCallId": invocation.call.id,
            }
            if invocation
            else {"kind": "direct"}
        )
        result = await self.call(
            "register_helper",
            {
                "sessionId": str(context_value("session_id", "")),
                "skill": skill.name,
                "source": path.read_text(encoding="utf-8"),
                "body": body,
                "kernelPath": str(path),
                "bodyPath": str(skill.directory / "SKILL.md"),
                "componentDigest": metadata.get(
                    "sourceFilesHash", "development-unverified"
                ),
                "registration": registration,
            },
        )
        return render_kernel_plugin_block(skill.name, result["exports"])

    async def aclose(self):
        await self.client.aclose()


class SSHNotebookContext:
    def __init__(self, binding, session_id):
        self.binding, self.session_id = binding, session_id

    async def prepare_request(self, history, tools, request_id):
        waiting = False
        try:
            while True:
                health = await self.binding.client.attach()
                if not {"notebook_model_v1", "notebook_r_v1"} <= set(
                    health.get("capabilities", ())
                ):
                    raise NotebookError("notebook_owner_upgrade_required", 409)
                owner = {k: health["owner"][k] for k in ("ownerId", "ownerEpoch")}
                result = await self.binding.call(
                    "context",
                    {"sessionId": self.session_id, "requestId": request_id},
                    owner=owner,
                )
                summary = result["summary"]
                active = summary["activeExecution"]
                if summary["state"]["controlMode"] != "user" and (
                    (active is None or active["status"] == "outcome_unknown")
                    and not summary.get("checkpoints", {}).get("waiting", False)
                ):
                    break
                if not waiting:
                    self.binding.waiters[self.session_id] = (
                        self.binding.waiters.get(self.session_id, 0) + 1
                    )
                    waiting = True
                await asyncio.sleep(0.25)
        finally:
            if waiting:
                count = self.binding.waiters.get(self.session_id, 1) - 1
                if count:
                    self.binding.waiters[self.session_id] = count
                else:
                    self.binding.waiters.pop(self.session_id, None)
        private = {"sessionId": self.session_id, "owner": owner, **result}
        # No token appears in messages; all source, paths and output remain user-role data.
        text = (
            "<notebook_context>\n"
            + NOTICE
            + "\nAfter creating/loading the main analysis object, register it with notebook_analysis. After changing a registered object, inspect it before another calculation. Use fresh ObjectRef/SelectionRef values; do not reuse an invalid selection or infer matrix semantics. State writes require a fresh model request before later scientific code. Report one completed key stage per cell with notebook_stage(stage, metadata): qc, normalization, integration, clustering, annotation or differential_expression. Saving captures the final successful cell state and reinspects registered objects. Read checkpoint failures and ask the person before continuing without a save; never infer consent."
            + "\n"
            + encoded(summary).decode().replace("<", "\\u003c").replace(">", "\\u003e")
            + "\n</notebook_context>"
        )
        if len(text.encode()) > MAX_CONTEXT_BYTES:
            raise NotebookError("notebook_response_limit", 413)
        return PreparedModelRequest(
            (Message(role=Role.USER, content=text),),
            ModelRequestContext(request_id, (("notebook", encoded(private).decode()),)),
        )

    async def augment(self, history, tools=()):
        raise RuntimeError("Notebook context requires model-request preparation")
