"""Scientific work runs outside the controller and uses its selected interpreter."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omicsclaw.entry.desktop.jobs_manager import JobError, JobFailure
from omicsclaw.entry.ssh.science_runner import ExternalScriptRunner
from omicsclaw.tools.context import ApprovalDecision, use_tool_context


def test_runner_uses_a_science_process_without_controller_imports(tmp_path):
    script = tmp_path / "process.py"
    script.write_text(
        "import json,os,sys;print(json.dumps({'pid':os.getpid(),'python':sys.executable}))"
    )
    observed = []

    class Approval:
        async def __call__(self, request):
            observed.append((request.tool_name, json.loads(request.arguments)))
            return ApprovalDecision(approved=True)

    async def run():
        runner = ExternalScriptRunner(Path(sys.executable), tmp_path)
        record = SimpleNamespace(
            inputs={"script": "process.py", "args": ["approved argument"]}
        )
        runner.validate("", record.inputs)
        ctx = SimpleNamespace(
            workspace=tmp_path,
            tool_started=lambda *a: None,
            tool_output=lambda *a: None,
            emit=lambda *a: None,
        )
        with use_tool_context(approval=Approval()):
            result = await runner(record, ctx)
        data = json.loads(result["output"])
        assert data["pid"] != os.getpid()
        assert Path(data["python"]).resolve() == Path(sys.executable).resolve()
        assert observed == [
            (
                "science_script",
                {
                    "script": str(script),
                    "interpreter": str(runner.interpreter),
                    "args": ["approved argument"],
                    "code_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                },
            )
        ]

    asyncio.run(run())


def test_runner_refuses_payload_escape(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (tmp_path / "outside.py").write_text("print('outside')")
    runner = ExternalScriptRunner(Path(sys.executable), payload)
    with pytest.raises(JobError, match="outside_payload"):
        runner.validate("", {"script": "../outside.py"})


def test_runner_refuses_code_changed_during_approval(tmp_path):
    script = tmp_path / "changed.py"
    script.write_text("print('approved')")

    class Approval:
        async def __call__(self, request):
            script.write_text("print('changed')")
            return ApprovalDecision(approved=True)

    async def run():
        runner = ExternalScriptRunner(Path(sys.executable), tmp_path)
        record = SimpleNamespace(inputs={"script": "changed.py"})
        with (
            use_tool_context(approval=Approval()),
            pytest.raises(JobFailure, match="changed after approval"),
        ):
            await runner(record, SimpleNamespace(workspace=tmp_path))

    asyncio.run(run())
