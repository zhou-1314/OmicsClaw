"""Small approval runner for browser acceptance; never mounted by production."""

import asyncio
import json
import sys

from omicsclaw.entry.desktop.jobs_manager import JobFailure
from omicsclaw.tools.context import require_approval


class ApprovalCsvRunner:
    def validate(self, skill, inputs):
        return None

    async def __call__(self, record, ctx):
        ctx.tool_started(
            "write_test_csv", human_description="Write the approved synthetic CSV"
        )
        ctx.progress("Waiting for approval", 10)
        decision = await require_approval(
            "write_test_csv",
            json.dumps({"output": "tables/approved.csv"}),
            reason="Write a two-row synthetic CSV in the test workspace",
        )
        if not decision.approved:
            raise JobFailure("approval denied", phase="approval")
        target = ctx.workspace / "tables" / "approved.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("label,value\\naccepted,1\\n")',
            str(target),
        )
        code = await process.wait()
        ctx.emit(
            "execution.finished",
            {"exit_code": code, "evidence_ref": "synthetic CSV writer process"},
        )
        if code:
            raise JobFailure("CSV writer failed")
        ctx.emit(
            "artifact.created",
            {
                "artifact_id": "approved-" + record.id,
                "path": str(target),
                "logical_output": "tables/approved.csv",
            },
        )
        ctx.progress("CSV ready", 100)
