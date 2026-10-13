import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from .models import Artifact


def build_manifest(
    path,
    *,
    run_id="",
    artifact_id=None,
    version=1,
    parent_artifact_id=None,
    job_id="",
    kernel_id="",
    host_id="",
    code_ref="",
    environment_ref="",
    input_artifact_ids=None,
):
    p = Path(path)
    digest = ""
    size = 0
    if not urlparse(str(path)).scheme and p.is_file():
        hasher = hashlib.sha256()
        with p.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
                size += len(chunk)
        digest = hasher.hexdigest()
    return Artifact(
        artifact_id=artifact_id or Artifact().artifact_id,
        run_id=run_id,
        uri=str(path),
        checksum=digest,
        size=size,
        content_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        version=version,
        parent_artifact_id=parent_artifact_id,
        job_id=job_id,
        kernel_id=kernel_id,
        host_id=host_id,
        code_ref=code_ref,
        environment_ref=environment_ref,
        input_artifact_ids=list(input_artifact_ids or []),
    )
