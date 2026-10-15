"""Verify one immutable supplied science environment before mounting its runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from omicsclaw.entry.env_supply import PinnedEnvironmentSupply

from .protocol import RpcError
from .science_payload import MAX_MANIFEST_BYTES, _copy_hash, verify_payload


def _owned_directory(value):
    if (
        not isinstance(value, str)
        or len(value.encode()) > 4096
        or not value.startswith("/")
        or any(part in {".", ".."} for part in value.split("/"))
        or any(character in value for character in "\0\r\n")
    ):
        raise RpcError("science_path_boundary")
    path = Path(value)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RpcError("science_path_boundary")
    observed = path.stat()
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_mode & 0o022
    ):
        raise RpcError("science_path_ownership")
    return path


def _read(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        before = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_size > limit
        ):
            raise RpcError("science_publication_boundary")
        raw = source.read(limit + 1)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        identity = tuple(getattr(before, field) for field in fields)
        if len(raw) != before.st_size or any(
            tuple(getattr(item, field) for field in fields) != identity
            for item in (os.fstat(source.fileno()), path.lstat())
        ):
            raise RpcError("science_publication_changed")
    return raw


@dataclass(frozen=True)
class ScienceDeployment:
    environment_root: Path
    environment_digest: str
    payload_root: Path
    payload_manifest: dict
    rscript: Path | None = None
    r_library: Path | None = None

    @property
    def python(self):
        return self.environment_root / "venv/bin/python"


def verified_science_deployment(document):
    allowed = {
        "schemaVersion",
        "environmentRoot",
        "environmentDigest",
        "payloadRoot",
        "payloadDigest",
        "payloadManifestSha256",
        "r",
    }
    if (
        not isinstance(document, dict)
        or set(document) != allowed
        or document["schemaVersion"] != 1
        or any(
            not isinstance(document[name], str)
            or not re.fullmatch(r"[a-f0-9]{64}", document[name])
            for name in ("environmentDigest", "payloadDigest", "payloadManifestSha256")
        )
    ):
        raise RpcError("invalid_science_snapshot")
    environment = _owned_directory(document["environmentRoot"])
    payload = _owned_directory(document["payloadRoot"])
    try:
        ready = json.loads(_read(environment / "ready.json", 64 * 1024))
    except FileNotFoundError as error:
        raise RpcError("science_environment_not_ready") from error
    manifest_raw = _read(environment / "manifest.json", 64 * 1024)
    manifest = json.loads(manifest_raw)
    digest = hashlib.sha256(manifest_raw).hexdigest()
    if (
        digest != document["environmentDigest"]
        or environment.name != digest
        or ready != {"environmentDigest": digest, "manifest": manifest}
        or set(manifest)
        != {
            "schemaVersion",
            "role",
            "pythonVersion",
            "platform",
            "machine",
            "abi",
            "lockSha256",
            "imports",
        }
        or manifest["schemaVersion"] != 1
        or manifest["role"] != "science"
        or json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        != manifest_raw
        or hashlib.sha256(
            _read(environment / "requirements.lock", 1024 * 1024)
        ).hexdigest()
        != manifest["lockSha256"]
    ):
        raise RpcError("science_environment_evidence_conflict")
    raw = _read(payload / "science-manifest.json", MAX_MANIFEST_BYTES)
    if hashlib.sha256(raw).hexdigest() != document["payloadManifestSha256"]:
        raise RpcError("science_payload_manifest_conflict")
    payload_manifest = json.loads(raw)
    verify_payload(payload, payload_manifest, expected_digest=document["payloadDigest"])
    rscript = library = None
    if document["r"] is not None:
        selected = document["r"]
        if (
            not isinstance(selected, dict)
            or set(selected) != {"rscriptPath", "rscriptSha256", "libraryRoot"}
            or not isinstance(selected["rscriptPath"], str)
            or not selected["rscriptPath"].startswith("/")
            or not re.fullmatch(r"[a-f0-9]{64}", str(selected["rscriptSha256"]))
        ):
            raise RpcError("invalid_science_r_snapshot")
        rscript = Path(selected["rscriptPath"])
        if any(part.is_symlink() for part in (rscript, *rscript.parents)):
            raise RpcError("science_r_path_boundary")
        if _copy_hash(rscript)[0] != selected["rscriptSha256"]:
            raise RpcError("science_r_evidence_conflict")
        library = _owned_directory(selected["libraryRoot"])
    # Scientific imports run in the selected process with its private HOME.
    PinnedEnvironmentSupply._verify(environment / "venv/bin/python", manifest)
    return ScienceDeployment(
        environment, digest, payload, payload_manifest, rscript, library
    )
