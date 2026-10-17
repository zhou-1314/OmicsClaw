"""Installed source verification; dependency installation remains the supplier's responsibility."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path


def _read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid component source file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("invalid component source file")
        return data
    finally:
        os.close(fd)


def verified_source_metadata(root: Path) -> dict:
    manifest_bytes = _read_regular(root / "controller-manifest.json", 1048576)
    manifest = json.loads(manifest_bytes)
    records = manifest.get("files")
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError("invalid component manifest")
    paths = set()
    size = 0
    for row in records:
        path = row.get("path")
        allowed = isinstance(path, str) and (
            path.startswith("omicsclaw/")
            or path.startswith("skills/")
            and path.endswith(("/SKILL.md", "/kernel.py"))
            or path in {"OMICSCLAW.md", "LICENSE"}
        )
        if (
            not allowed
            or any(part in {"", "..", "."} for part in path.split("/"))
            or path in paths
        ):
            raise ValueError("invalid component source path")
        paths.add(path)
        source = root / path
        if source.resolve().parent != source.parent.resolve() or any(
            part.is_symlink()
            for part in [source, *source.parents]
            if part != root.parent
        ):
            raise ValueError("component source symlink refused")
        data = _read_regular(source, 4718592)
        if len(data) != row.get("bytes") or hashlib.sha256(data).hexdigest() != row.get(
            "sha256"
        ):
            raise ValueError("component source file mismatch")
        size += len(data)
        if size > 4718592:
            raise ValueError("component source exceeds budget")
    digest = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != manifest.get("sourceFilesSha256") or size != manifest.get(
        "sourceBytes"
    ):
        raise ValueError("component source manifest mismatch")
    if not re.fullmatch(
        r"[0-9a-f]{64}", manifest.get("archiveSha256", "")
    ) or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("sourceCommit", "")):
        raise ValueError("invalid component source identity")
    return {
        "componentVersion": manifest["componentVersion"],
        "protocolVersion": manifest["protocolVersion"],
        "sourceArchiveHash": manifest["archiveSha256"],
        "sourceFilesHash": digest,
        "sourceManifestHash": hashlib.sha256(manifest_bytes).hexdigest(),
        "sourceCommit": manifest["sourceCommit"],
        "sourceCommitVerified": manifest.get("sourceCommitVerified") is True,
        "manifestVerified": True,
    }
