"""Complete skill code/resources supplied separately from the controller."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

MAX_FILES = 20_000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _files(root: Path, *, installed: bool = False):
    for directory, folders, names in os.walk(root / "skills", followlinks=False):
        for name in (*folders, *names):
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise ValueError("science_payload_symlink_refused")
            if installed and (
                name.startswith(".")
                or name in {"__pycache__", "tests"}
                or name.endswith((".pyc", ".pyo"))
            ):
                raise ValueError("science_payload_integrity")
        folders[:] = sorted(
            name
            for name in folders
            if name not in {"__pycache__", ".git", ".pytest_cache", "tests"}
        )
        for name in sorted(names):
            if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                continue
            yield Path(directory) / name


def _copy_hash(path: Path, destination: Path | None = None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    copied = 0
    with os.fdopen(os.open(path, flags), "rb") as reader:
        before = os.fstat(reader.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError("science_payload_file_budget")
        sink = destination.open("xb") if destination else None
        try:
            for chunk in iter(lambda: reader.read(64 * 1024), b""):
                copied += len(chunk)
                if copied > MAX_FILE_BYTES:
                    raise ValueError("science_payload_file_budget")
                digest.update(chunk)
                if sink:
                    sink.write(chunk)
            after = os.fstat(reader.fileno())
            observed = path.stat()
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            identity = tuple(getattr(before, key) for key in fields)
            if copied != before.st_size or any(
                tuple(getattr(item, key) for key in fields) != identity
                for item in (after, observed)
            ):
                raise ValueError("science_payload_source_changed")
        finally:
            if sink:
                sink.close()
    return digest.hexdigest(), copied


def _digest(records):
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_payload(source: Path, destination: Path):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError("science_payload_destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".science-payload-", dir=destination.parent)
    )
    records = []
    total = 0
    try:
        for path in _files(source):
            relative = path.relative_to(source).as_posix()
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = _copy_hash(path, target)
            total += size
            records.append({"path": relative, "bytes": size, "sha256": digest})
            if len(records) > MAX_FILES or total > MAX_PAYLOAD_BYTES:
                raise ValueError("science_payload_budget")
        records.sort(key=lambda record: record["path"])
        if not records:
            raise ValueError("science_payload_empty")
        manifest = {
            "schemaVersion": 1,
            "files": records,
            "sourceFilesSha256": _digest(records),
            "sourceBytes": total,
        }
        encoded = json.dumps(manifest, sort_keys=True).encode()
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("science_payload_manifest_budget")
        (temporary / "science-manifest.json").write_bytes(encoded)
        os.rename(temporary, destination)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_payload(root: Path, manifest, *, expected_digest: str):
    root = Path(root).resolve(strict=True)
    encoded = json.dumps(manifest, sort_keys=True).encode()
    records = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        len(encoded) > MAX_MANIFEST_BYTES
        or not isinstance(records, list)
        or not records
        or len(records) > MAX_FILES
    ):
        raise ValueError("science_payload_manifest_budget")
    if (
        manifest.get("schemaVersion") != 1
        or _digest(records) != expected_digest
        or manifest.get("sourceFilesSha256") != expected_digest
    ):
        raise ValueError("science_payload_integrity")
    expected = {}
    total = 0
    for record in records:
        name = record.get("path", "")
        path = Path(name)
        if (
            not name.startswith("skills/")
            or path.is_absolute()
            or ".." in path.parts
            or name in expected
        ):
            raise ValueError("science_payload_integrity")
        target = root / path
        if target.resolve(strict=True) != target or target.is_symlink():
            raise ValueError("science_payload_integrity")
        digest, size = _copy_hash(target)
        if (digest, size) != (record.get("sha256"), record.get("bytes")):
            raise ValueError("science_payload_integrity")
        total += size
        if total > MAX_PAYLOAD_BYTES:
            raise ValueError("science_payload_budget")
        expected[name] = True
    actual = {
        path.relative_to(root).as_posix() for path in _files(root, installed=True)
    }
    if actual != set(expected) or total != manifest.get("sourceBytes"):
        raise ValueError("science_payload_integrity")
    return total
