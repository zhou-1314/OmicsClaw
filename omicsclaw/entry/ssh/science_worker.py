"""A finite worker calling the original skill API runner in its own process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from omicsclaw.entry.desktop.jobs_manager import (
    JobExecutionContext,
    JobRecord,
    SkillApiRunner,
)
from omicsclaw.skills.loader import load_skills

from .science_payload import verify_payload

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_EVENTS_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024


async def execute(request_path: Path):
    with request_path.open("rb") as source:
        raw = source.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("science_request_budget")
    request = json.loads(raw)
    if request.get("schemaVersion") != 1:
        raise ValueError("science_request_version")
    workspace = Path(request["workspace"]).resolve(strict=True)
    if workspace != Path.cwd().resolve() or not request_path.resolve().is_relative_to(
        workspace
    ):
        raise ValueError("science_worker_workspace_mismatch")
    payload = Path(request["payloadRoot"]).resolve(strict=True)
    verify_payload(
        payload, request["payloadManifest"], expected_digest=request["payloadDigest"]
    )
    sys.dont_write_bytecode = True
    # Scientific imports happen inside SkillApiRunner in this process only.
    sys.path.insert(0, str(payload))
    app = SimpleNamespace(skills=load_skills(payload / "skills"))
    runner = SkillApiRunner(app)
    runner.validate(request["skill"], request["inputs"])
    record = JobRecord(
        id=request["jobId"], skill=request["skill"], inputs=request["inputs"]
    )
    total = 0
    sequence = 0
    with (workspace / "_omicsclaw_worker_events.jsonl").open(
        "x", encoding="utf-8"
    ) as stream:

        def emit(event_type, value):
            nonlocal total, sequence
            sequence += 1
            encoded = (
                json.dumps(
                    {"seq": sequence, "type": event_type, "payload": value},
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )
            size = len(encoded.encode())
            total += size
            if size > MAX_EVENT_BYTES or total > MAX_EVENTS_BYTES:
                raise ValueError("science_event_budget")
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            return sequence

        await runner(record, JobExecutionContext(record, emit, workspace))
    from omicsclaw.entry.desktop.artifacts_manager import (
        _is_fallback_output,
        _match_declarations,
        _read_contract,
        scan_workspace_files,
    )

    from .science_payload import _copy_hash

    found = app.skills.get(record.skill.rsplit("/", 1)[-1])
    files = scan_workspace_files(workspace)
    declared = _read_contract(found.directory)
    matches = _match_declarations(declared, files)
    if not matches:
        matches = [
            (None, path)
            for name, path in sorted(files.items())
            if _is_fallback_output(name)
        ]
    artifacts = []
    total = 0
    for declaration, path in matches:
        if (
            len(artifacts) >= 2000
            or path.is_symlink()
            or not path.resolve().is_relative_to(workspace)
        ):
            raise ValueError("science_artifact_boundary")
        digest, size = _copy_hash(path)
        total += size
        if total > 1024 * 1024 * 1024:
            raise ValueError("science_artifact_quota")
        artifacts.append(
            {
                "path": path.relative_to(workspace).as_posix(),
                "sha256": digest,
                "bytes": size,
                "declared": declaration,
            }
        )
    (workspace / "_omicsclaw_artifacts.json").write_text(json.dumps(artifacts))
    proof = {
        "pid": os.getpid(),
        "python": sys.executable,
        "payloadDigest": request["payloadDigest"],
        "events": sequence,
    }
    temporary = workspace / "_omicsclaw_worker.json.tmp"
    temporary.write_text(json.dumps(proof))
    os.replace(temporary, workspace / "_omicsclaw_worker.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    arguments = parser.parse_args()
    asyncio.run(execute(arguments.request))


if __name__ == "__main__":
    main()
