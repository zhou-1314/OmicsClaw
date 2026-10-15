"""A finite worker calling the original skill API runner in its own process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import tempfile
from functools import partial
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


def write_result(value, target: Path, suffix: str) -> None:
    """Write a scientific return using the established file-format conventions."""
    def json_value(item):
        if hasattr(item, "tolist"):
            return item.tolist()
        if hasattr(item, "item"):
            return item.item()
        if isinstance(item, Path):
            return str(item)
        raise TypeError(f"{type(item).__name__} is not JSON serialisable")

    suffix = suffix.lower()
    if suffix == ".h5ad" and hasattr(value, "write_h5ad"):
        value.write_h5ad(target)
    elif suffix in {".csv", ".tsv"} and hasattr(value, "to_csv"):
        index = getattr(value, "index", None)
        keep_index = not (index is not None and type(index).__name__ == "RangeIndex")
        value.to_csv(target, sep="\t" if suffix == ".tsv" else ",", index=keep_index)
    elif suffix == ".parquet" and hasattr(value, "to_parquet"):
        value.to_parquet(target)
    elif suffix in {".png", ".pdf", ".svg"} and hasattr(value, "savefig"):
        value.savefig(target, dpi=150, bbox_inches="tight", format=suffix[1:])
    elif suffix == ".json" and isinstance(value, (dict, list)):
        target.write_text(json.dumps(value, indent=2, default=json_value) + "\n", encoding="utf-8")
    elif suffix in {".md", ".txt"} and isinstance(value, str):
        target.write_text(value, encoding="utf-8")
    else:
        raise ValueError(f"no writer for {type(value).__name__} as {suffix}")


def persist_result(result, workspace, *, writer):
    """Serialize a return inside the worker and publish its files atomically."""
    if hasattr(result, "write_h5ad"):
        suffix = ".h5ad"
    elif hasattr(result, "to_csv"):
        suffix = ".csv"
    elif hasattr(result, "savefig"):
        suffix = ".png"
    elif isinstance(result, (dict, list)):
        suffix = ".json"
    elif isinstance(result, str):
        suffix = ".txt"
    else:
        return
    directory = workspace / "tables"
    if directory.is_symlink():
        raise ValueError("science_result_boundary")
    directory.mkdir(exist_ok=True)
    if directory.resolve(strict=True).parent != workspace:
        raise ValueError("science_result_boundary")
    values = [("omicsclaw-result" + suffix, result, suffix)]
    attributes = getattr(result, "attrs", {})
    if isinstance(attributes, dict) and attributes.get("run_info"):
        values.append(("omicsclaw-result-info.json", attributes["run_info"], ".json"))
    for name, value, extension in values:
        target = directory / name
        if target.exists() or target.is_symlink():
            raise ValueError("science_result_conflict")
        descriptor, temporary = tempfile.mkstemp(prefix=".result-", dir=directory)
        os.close(descriptor)
        path = Path(temporary)
        try:
            writer(value, path, extension)
            if path.stat().st_size > 1024 * 1024 * 1024:
                raise ValueError("science_result_quota")
            with path.open("rb") as source:
                os.fsync(source.fileno())
            os.replace(path, target)
        finally:
            path.unlink(missing_ok=True)


async def execute(request_path: Path, *, result_writer):
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
    from .science_payload import _copy_hash

    for item in request.get("dataFiles", []):
        path, _ = SkillApiRunner.file_marker({"$file": item["path"]}, workspace)
        digest, size = _copy_hash(path)
        if digest != item["sha256"] or size != item["bytes"]:
            raise ValueError("science_input_integrity")
    # Scientific imports happen inside SkillApiRunner in this process only.
    sys.path.insert(0, str(payload))
    app = SimpleNamespace(skills=load_skills(payload / "skills"))
    runner = SkillApiRunner(
        app, result_writer=partial(persist_result, writer=result_writer)
    )
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
        "rssPeakBytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
        "childRssPeakBytes": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
        "scientificImports": sorted(
            {
                "numpy",
                "pandas",
                "scipy",
                "sklearn",
                "scanpy",
                "anndata",
                "torch",
            }.intersection(sys.modules)
        ),
    }
    temporary = workspace / "_omicsclaw_worker.json.tmp"
    temporary.write_text(json.dumps(proof))
    os.replace(temporary, workspace / "_omicsclaw_worker.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    arguments = parser.parse_args()
    asyncio.run(execute(arguments.request, result_writer=write_result))


if __name__ == "__main__":
    main()
