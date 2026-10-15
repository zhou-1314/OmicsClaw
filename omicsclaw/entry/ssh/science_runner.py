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


class ScienceJobRunner:
    """Prepare immutable science inputs and use the original external job plane."""

    lifetime = "external"
    binds_scientific_inputs = True

    def __init__(
        self,
        app,
        bridge,
        *,
        interpreter: Path,
        component_root: Path,
        payload_root: Path,
        manifest,
        environment_digest: str,
        rscript: Path | None = None,
        r_library: Path | None = None,
        default_hard_timeout_s: float | None = None,
    ):
        import re

        from .science_payload import verify_payload

        if not re.fullmatch(r"[a-f0-9]{64}", environment_digest):
            raise JobError("science_environment_digest_required")
        self._app = app
        self._bridge = bridge
        self.target_alias = bridge.target_alias
        self.interpreter = interpreter.absolute()
        if not self.interpreter.is_file():
            raise JobError("science_interpreter_unavailable")
        self.component_root = component_root.resolve(strict=True)
        self.payload_root = payload_root.resolve(strict=True)
        self.manifest = json.loads(json.dumps(manifest))
        self.payload_digest = self.manifest["sourceFilesSha256"]
        verify_payload(
            self.payload_root, self.manifest, expected_digest=self.payload_digest
        )
        self.environment_digest = environment_digest
        from omicsclaw.remote.jobs import validate_hard_timeout

        self.default_hard_timeout_s = validate_hard_timeout(default_hard_timeout_s)
        self.rscript = rscript.absolute() if rscript else None
        self.r_library = r_library.resolve(strict=True) if r_library else None
        if self.rscript and (not self.rscript.is_file() or self.r_library is None):
            raise JobError("science_r_binding_incomplete")

    async def resolve_target(self, alias, **kwargs):
        return await self._bridge.resolve_target(alias, **kwargs)

    def canonical_inputs(self, skill, inputs):
        from omicsclaw.entry.desktop.jobs_manager import SkillApiRunner

        if "_science" in inputs:
            raise JobError("reserved_science_inputs")
        SkillApiRunner(self._app).validate(skill, inputs)
        files = []
        for value in inputs.values():
            if isinstance(value, dict) and "$file" in value:
                from .science_payload import _copy_hash

                path, _ = SkillApiRunner.file_marker(
                    value, Path(self._app.config.workspace)
                )
                digest, size = _copy_hash(path)
                item = {"path": value["$file"], "sha256": digest, "bytes": size}
                if item not in files:
                    files.append(item)
        files.sort(key=lambda item: item["path"])
        return {
            **inputs,
            "_science": {
                "schemaVersion": 1,
                "python": str(self.interpreter),
                "environmentDigest": self.environment_digest,
                "payloadRoot": str(self.payload_root),
                "payloadDigest": self.payload_digest,
                "componentRoot": str(self.component_root),
                "rscript": str(self.rscript) if self.rscript else "",
                "rLibrary": str(self.r_library) if self.r_library else "",
                "dataFiles": files,
            },
        }

    def validate(self, skill, inputs):
        from omicsclaw.entry.desktop.jobs_manager import SkillApiRunner

        SkillApiRunner(self._app).validate(
            skill, {key: value for key, value in inputs.items() if key != "_science"}
        )
        if (
            inputs.get("_science")
            != self.canonical_inputs(
                skill,
                {key: value for key, value in inputs.items() if key != "_science"},
            )["_science"]
        ):
            raise JobError("science_snapshot_conflict")

    async def __call__(self, record, ctx):
        from omicsclaw.remote.ssh import shell_quote

        from .science_payload import verify_payload

        self.validate(record.skill, record.inputs)
        snapshot = record.inputs["_science"]
        request = {
            "schemaVersion": 1,
            "jobId": record.id,
            "skill": record.skill,
            "inputs": {
                key: value for key, value in record.inputs.items() if key != "_science"
            },
            "payloadRoot": snapshot["payloadRoot"],
            "workspace": record.target_workdir,
            "payloadManifest": self.manifest,
            "payloadDigest": snapshot["payloadDigest"],
            "dataFiles": snapshot["dataFiles"],
        }
        content = json.dumps(
            request, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        if len(content.encode()) > 2 * 1024 * 1024:
            raise JobError("science_request_budget")
        approval = json.dumps(
            {
                "skill": record.skill,
                "target": record.target_alias,
                "workdir": record.target_workdir,
                "hard_timeout_s": record.hard_timeout_s,
                **snapshot,
            },
            sort_keys=True,
        )
        from omicsclaw.tools.context import use_effective_policy, use_timeout_pause

        with use_effective_policy(None), use_timeout_pause(None):
            await require_approval(
                "science_job",
                approval,
                reason=(
                    "Run this skill in the selected scientific environment and workspace."
                    + (
                        f" Execution limit: {record.hard_timeout_s:g} seconds."
                        if record.hard_timeout_s is not None
                        else ""
                    )
                ),
            )
        await asyncio.to_thread(
            verify_payload,
            self.payload_root,
            self.manifest,
            expected_digest=self.payload_digest,
        )
        if (
            self.canonical_inputs(
                record.skill,
                {
                    key: value
                    for key, value in record.inputs.items()
                    if key != "_science"
                },
            )["_science"]
            != snapshot
        ):
            raise JobError("science_inputs_changed_after_approval")
        environment = {
            "PATH": str(self.interpreter.parent) + ":/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.component_root) + ":" + str(self.payload_root),
            "HOME": record.target_workdir + "/.science-home",
            "TMPDIR": record.target_workdir + "/.science-tmp",
            "RETICULATE_PYTHON": str(self.interpreter),
            "RETICULATE_USE_MANAGED_VENV": "no",
        }
        if self.rscript:
            environment.update(
                {
                    "OMICSCLAW_RSCRIPT": str(self.rscript),
                    "OMICSCLAW_R_LIBS": str(self.r_library),
                    "R_LIBS_USER": str(self.r_library),
                }
            )
        command = (
            "mkdir -p -- .science-home .science-tmp && env -i "
            + " ".join(
                shell_quote(key + "=" + value) for key, value in environment.items()
            )
            + " "
            + shell_quote(str(self.interpreter))
            + " -B -m omicsclaw.entry.ssh.science_worker --request science-request.json"
        )
        outcome = await self._bridge.plane.submit(
            record.target_alias,
            command,
            scheduler="none",
            hard_timeout_s=record.hard_timeout_s,
            deadline_python=str(self.interpreter),
            local_job_id=record.id,
            target_workdir=record.target_workdir,
            supplied_files={"science-request.json": content},
            inputs=tuple(
                (item["path"], item["path"]) for item in snapshot["dataFiles"]
            ),
        )
        result = await self._bridge.observe(outcome, record, ctx, command=approval)
        if result is None:
            row = self._bridge.plane.job_store().get(outcome.job_ref)
            await self.recover_evidence(record, row, ctx)
        return result

    async def _read_evidence(self, row, name, maximum):
        from omicsclaw.remote.ssh import shell_quote

        path = row.workdir.rstrip("/") + "/" + name
        quoted = shell_quote(path)
        outcome = await self._bridge.plane.executor_for(row.host_alias).run(
            f"[ -f {quoted} ] && [ ! -L {quoted} ] && "
            f'[ "$(realpath -- {quoted})" = {quoted} ] && '
            f'[ "$(stat -c %s -- {quoted})" -le {maximum} ] && '
            f"head -c {maximum + 1} -- {quoted}",
            timeout=30,
        )
        if outcome.exit_code or len(outcome.output.encode()) > maximum:
            raise JobError("science_evidence_unconfirmed")
        return outcome.output

    async def recover_evidence(self, record, row, ctx):
        import re
        from pathlib import PurePosixPath

        from omicsclaw.remote.ssh import shell_quote

        proof = json.loads(
            await self._read_evidence(row, "_omicsclaw_worker.json", 64 * 1024)
        )
        if proof.get("payloadDigest") != record.inputs["_science"]["payloadDigest"]:
            raise JobError("science_evidence_conflict")
        raw = await self._read_evidence(
            row, "_omicsclaw_worker_events.jsonl", 4 * 1024 * 1024
        )
        events = raw.splitlines()
        if len(events) != proof.get("events") or len(events) > 20_000:
            raise JobError("science_evidence_conflict")
        for expected, line in enumerate(events, 1):
            value = json.loads(line)
            if (
                len(line.encode()) > 64 * 1024
                or value.get("seq") != expected
                or value.get("type")
                not in {"tool_started", "progress", "tool_output_chunk"}
                or not isinstance(value.get("payload"), dict)
            ):
                raise JobError("science_evidence_conflict")
            ctx.emit(
                value["type"],
                {**value["payload"], "source_event_id": f"science:{row.id}:{expected}"},
            )
        artifacts = json.loads(
            await self._read_evidence(row, "_omicsclaw_artifacts.json", 2 * 1024 * 1024)
        )
        if not isinstance(artifacts, list) or len(artifacts) > 2000:
            raise JobError("science_artifact_budget")
        total = 0
        for number, artifact in enumerate(artifacts):
            relative = PurePosixPath(artifact.get("path", ""))
            digest, size = artifact.get("sha256", ""), artifact.get("bytes")
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(size) is not int
                or size < 0
            ):
                raise JobError("science_artifact_boundary")
            total += size
            if total > 1024 * 1024 * 1024:
                raise JobError("science_artifact_budget")
            path = row.workdir.rstrip("/") + "/" + str(relative)
            quoted = shell_quote(path)
            checked = await self._bridge.plane.executor_for(row.host_alias).run(
                f"[ -f {quoted} ] && [ ! -L {quoted} ] && "
                f'[ "$(realpath -- {quoted})" = {quoted} ] && '
                f'[ "$(stat -c %s -- {quoted})" = {size} ] && '
                f"_hash=$(sha256sum -- {quoted}) && _hash=${{_hash%% *}} && "
                f'[ "$_hash" = {shell_quote(digest)} ]',
                timeout=30,
            )
            if checked.exit_code:
                raise JobError("science_artifact_integrity")
            ctx.emit(
                "artifact.created",
                {
                    "remote_ref": f"remote://{row.host_alias}{path}",
                    "host_id": row.host_alias,
                    "logical_output": str(relative),
                    "verified_sha256": digest,
                    "verified_bytes": size,
                    "environment_ref": record.inputs["_science"]["environmentDigest"],
                    "source_event_id": f"science:{row.id}:artifact:{number}",
                },
            )


class RunSkillJobTool:
    """The agent face admits through the same manager as the skill form."""

    name = "run_skill_job"

    def __init__(self, jobs):
        self._jobs = jobs

    def definition(self):
        from omicsclaw.schema import ToolDefinition

        return ToolDefinition(
            self.name,
            "Admit a skill job in the selected scientific environment. The job's approval card must be approved before execution. Observe the returned job ID; repeating the same source request does not execute again.",
            {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "inputs": {"type": "object"},
                    "source_request_id": {"type": "string"},
                    "runtime": {"type": "string"},
                    "hard_timeout_s": {
                        "type": "number",
                        "minimum": 0.001,
                        "maximum": 604800,
                    },
                },
                "required": ["skill", "source_request_id"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments):
        from omicsclaw.entry.desktop.job_service import create_job_from_document
        from omicsclaw.tools.context import context_value
        from omicsclaw.tools.function_tool import (
            ToolArgumentError,
            decode_arguments,
            validate_arguments,
        )

        document = decode_arguments(arguments)
        issues = validate_arguments(document, self.definition().input_schema)
        if issues:
            raise ToolArgumentError("invalid skill job request: " + "; ".join(issues))
        record = await create_job_from_document(
            self._jobs() if callable(self._jobs) else self._jobs,
            {
                **document,
                "kind": "skill_run",
                "source_request_id": "agent:" + document["source_request_id"],
                "session_id": str(context_value("session_id", "")),
            },
        )
        return json.dumps(
            {"job_id": record.id, "status": record.status, "lifetime": record.lifetime}
        )
