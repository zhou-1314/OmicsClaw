"""Read registered execution images through bounded, versioned file descriptors."""

import base64
import hashlib
import os
import stat
from pathlib import Path

from .contracts import encoded
from .service import NotebookError, identifier

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_CHUNK_BYTES = 128 * 1024


def open_inside(workspace, path):
    try:
        parts = Path(path).relative_to(workspace).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("path")
        directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            return os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        finally:
            os.close(directory)
    except (OSError, ValueError):
        raise NotebookError("notebook_artifact_unavailable", 409) from None


def evidence(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


class OwnerArtifacts:
    def __init__(self, runtime):
        self.r = runtime

    def read(self, params, *, chunk=False):
        fields = {"notebookId", "executionId", "artifactId"}
        if chunk:
            fields |= {"version", "offset", "length"}
        if set(params) != fields:
            raise NotebookError("invalid_notebook_command")
        for name in ("notebookId", "executionId", "artifactId"):
            identifier(params[name])
        if chunk and (
            not isinstance(params["version"], str)
            or len(params["version"]) != 64
            or type(params["offset"]) is not int
            or not 0 <= params["offset"] <= MAX_IMAGE_BYTES
            or type(params["length"]) is not int
            or not 1 <= params["length"] <= MAX_CHUNK_BYTES
        ):
            raise NotebookError("invalid_notebook_command")
        record = self.r.artifacts.get_artifact(params["artifactId"])
        execution = self.r.db.run(
            lambda c: c.execute(
                "SELECT k.session_key FROM notebook_executions e JOIN notebook_runtime_executions x USING(execution_id) "
                "JOIN notebook_runtime_kernels k ON k.instance_id=x.instance_id WHERE e.notebook_id=? AND e.execution_id=?",
                (params["notebookId"], params["executionId"]),
            ).fetchone()
        )
        if (
            not record
            or not execution
            or record.session_id != execution[0]
            or record.meta.get("executionId") != params["executionId"]
            or record.mime != "image/png"
        ):
            raise NotebookError("notebook_artifact_unavailable", 409)
        def _read_and_verify(target_path):
            fd = open_inside(self.r.workspace, target_path)
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.getuid()
                    or not 0 < before.st_size <= MAX_IMAGE_BYTES
                ):
                    raise NotebookError("notebook_artifact_unavailable", 409)
                digest = hashlib.sha256()
                header = os.read(fd, 8)
                if header != b"\x89PNG\r\n\x1a\n":
                    raise NotebookError("notebook_artifact_unavailable", 409)
                digest.update(header)
                total = 8
                while total <= MAX_IMAGE_BYTES:
                    block = os.read(fd, MAX_CHUNK_BYTES)
                    if not block:
                        break
                    total += len(block)
                    digest.update(block)
                if (
                    total != before.st_size
                    or evidence(before) != evidence(os.fstat(fd))
                    or digest.hexdigest() != record.sha256
                    or total != record.size
                ):
                    raise NotebookError("notebook_artifact_changed", 409)
                return fd, before, digest, total
            except Exception:
                os.close(fd)
                raise

        try:
            fd, before, digest, total = _read_and_verify(record.path)
        except NotebookError as err:
            snapshot_path = record.meta.get("snapshotPath") if isinstance(record.meta, dict) else None
            overwritten = (
                bool(snapshot_path)
                and err.code == "notebook_artifact_changed"
                and self.r.artifact_db.run(
                    lambda c: c.execute(
                        "SELECT 1 FROM artifacts WHERE path = ? AND id != ? LIMIT 1",
                        (record.path, record.id),
                    ).fetchone()
                )
                is not None
            )
            if not overwritten:
                raise
            fd, before, digest, total = _read_and_verify(snapshot_path)
        try:
            version = hashlib.sha256(
                encoded([evidence(before), digest.hexdigest()])
            ).hexdigest()
            if not chunk:
                return {
                    "artifactId": record.id,
                    "executionId": params["executionId"],
                    "mime": record.mime,
                    "size": total,
                    "sha256": digest.hexdigest(),
                    "version": version,
                }
            if version != params["version"] or params["offset"] > total:
                raise NotebookError("notebook_artifact_changed", 409)
            data = os.pread(fd, params["length"], params["offset"])
            if evidence(before) != evidence(os.fstat(fd)):
                raise NotebookError("notebook_artifact_changed", 409)
            offset = params["offset"] + len(data)
            return {
                "artifactId": record.id,
                "version": version,
                "offset": params["offset"],
                "dataBase64": base64.b64encode(data).decode(),
                "nextOffset": offset,
                "eof": offset == total,
            }
        finally:
            os.close(fd)
