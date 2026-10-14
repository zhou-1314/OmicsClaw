"""An explicitly bound science process behind JobsManager.register_runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
from pathlib import Path

from omicsclaw.entry.desktop.jobs_manager import JobError, JobFailure
from omicsclaw.tools.context import require_approval


class ExternalScriptRunner:
    def __init__(self, interpreter: Path, payload_root: Path):
        interpreter.resolve(strict=True)
        self.interpreter = interpreter.absolute()
        self.payload_root = payload_root.resolve(strict=True)

    def validate(self, skill, inputs):
        if not isinstance(inputs.get("script"), str) or not isinstance(
            inputs.get("args", []), list
        ):
            raise JobError("science_script_required")
        self._script(inputs["script"])
        if any(not isinstance(value, str) for value in inputs.get("args", [])):
            raise JobError("science_arguments_must_be_strings")

    def _script(self, relative):
        path = (self.payload_root / relative).resolve(strict=True)
        if not path.is_relative_to(self.payload_root) or not path.is_file():
            raise JobError("science_script_outside_payload")
        return path

    async def __call__(self, record, ctx):
        script = self._script(record.inputs["script"])
        arguments = [str(self.interpreter), str(script), *record.inputs.get("args", [])]
        code_hash = hashlib.sha256(script.read_bytes()).hexdigest()
        approval_arguments = json.dumps(
            {
                "script": str(script),
                "interpreter": str(self.interpreter),
                "args": arguments[2:],
                "code_sha256": code_hash,
            },
            sort_keys=True,
        )
        await require_approval(
            "science_script",
            approval_arguments,
            reason="Run this script in the independently selected scientific environment.",
        )
        if hashlib.sha256(script.read_bytes()).hexdigest() != code_hash:
            raise JobFailure("science script changed after approval", phase="run")
        ctx.tool_started("science_script", approval_arguments)
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=ctx.workspace,
            env={
                "PATH": str(self.interpreter.parent) + ":/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        output = bytearray()
        try:
            while chunk := await process.stdout.read(32768):
                if len(output) + len(chunk) > 1024 * 1024:
                    raise JobFailure("science output exceeds limit", phase="run")
                output.extend(chunk)
            code = await process.wait()
            ctx.emit(
                "execution.finished",
                {
                    "exit_code": code,
                    "evidence_ref": "science-process:" + str(process.pid),
                },
            )
            text = output.decode("utf-8", errors="replace")
            ctx.tool_output(text)
            if code:
                raise JobFailure("science script failed", phase="run")
            return {
                "exit_code": code,
                "output": text,
                "interpreter": str(self.interpreter),
            }
        finally:
            if process.returncode is None:
                import os

                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
