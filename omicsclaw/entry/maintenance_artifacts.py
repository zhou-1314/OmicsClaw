"""Bounded, private copies of typed local package inputs before approval."""

from __future__ import annotations

import email.parser
import hashlib
import os
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from .environment_coordination import CoordinationError
from .notebook.security import private_directory, private_file

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,95}")


def invalid():
    return CoordinationError("environment_maintenance_invalid_artifact")


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_operation(value, language):
    if not isinstance(value, dict):
        raise CoordinationError("environment_maintenance_invalid_plan")
    expected = {"adapter", "artifacts", "checks"}
    if language == "r":
        expected.add("libraryPath")
    if (
        set(value) != expected
        or value["adapter"] != {"python": "python_wheels", "r": "r_source"}[language]
    ):
        raise CoordinationError("environment_maintenance_unsupported_adapter")
    if (
        not isinstance(value["artifacts"], list)
        or not 1 <= len(value["artifacts"]) <= 8
    ):
        raise invalid()
    if not isinstance(value["checks"], list) or not 1 <= len(value["checks"]) <= 32:
        raise CoordinationError("environment_maintenance_invalid_plan")
    for check in value["checks"]:
        keys = (
            {"package", "version", "module"}
            if language == "python"
            else {"package", "version"}
        )
        if (
            not isinstance(check, dict)
            or set(check) != keys
            or not NAME.fullmatch(str(check["package"]))
            or not VERSION.fullmatch(str(check["version"]))
        ):
            raise CoordinationError("environment_maintenance_invalid_plan")
        if language == "python" and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", str(check["module"])
        ):
            raise CoordinationError("environment_maintenance_invalid_plan")
    return value


def _safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name:
        raise invalid()


def _metadata(path, language):
    total, count = 0, 0
    if language == "python":
        with zipfile.ZipFile(path) as archive:
            records = archive.infolist()
            metadata = []
            for member in records:
                _safe_member(member.filename)
                total += member.file_size
                count += 1
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise invalid()
                if member.filename.endswith(".dist-info/METADATA"):
                    metadata.append(member)
            if (
                count > MAX_MEMBERS
                or total > MAX_EXPANDED_BYTES
                or len(metadata) != 1
                or metadata[0].file_size > 65536
            ):
                raise invalid()
            document = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
            return {
                "name": document.get("Name"),
                "version": document.get("Version"),
                "requires": document.get_all("Requires-Dist", []),
                "requiresPython": document.get("Requires-Python", ""),
                "filename": path.name,
            }
    with tarfile.open(path, mode="r:gz") as archive:
        descriptions = []
        for member in archive:
            _safe_member(member.name)
            total += member.size
            count += 1
            if (
                count > MAX_MEMBERS
                or total > MAX_EXPANDED_BYTES
                or not (member.isdir() or member.isfile())
            ):
                raise invalid()
            if len(PurePosixPath(member.name).parts) == 2 and member.name.endswith(
                "/DESCRIPTION"
            ):
                if member.size > 65536:
                    raise invalid()
                descriptions.append(
                    archive.extractfile(member).read(65537).decode("utf-8")
                )
        if len(descriptions) != 1:
            raise invalid()
    fields = {}
    key = None
    for line in descriptions[0].splitlines():
        if line[:1].isspace() and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            if key in fields:
                raise invalid()
            fields[key] = value.strip()
    return {
        "name": fields.get("Package"),
        "version": fields.get("Version"),
        "requires": ",".join(
            fields.get(k, "") for k in ("Depends", "Imports", "LinkingTo")
        ),
        "filename": path.name,
    }


def prepare_artifacts(operation, language, directory):
    """The installer receives these copies, never the caller's replaceable paths."""
    validate_operation(operation, language)
    directory = private_directory(Path(directory))
    prepared, names, total = [], set(), 0
    for item in operation["artifacts"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item["path"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", str(item["sha256"]))
        ):
            raise invalid()
        source = Path(item["path"])
        suffix = ".whl" if language == "python" else ".tar.gz"
        if (
            not source.is_absolute()
            or not source.name.endswith(suffix)
            or source.name in names
            or len(source.name.encode()) > 255
        ):
            raise invalid()
        names.add(source.name)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        target = directory / source.name
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or not 1 <= before.st_size <= MAX_ARTIFACT_BYTES
            ):
                raise invalid()
            out = private_file(target)
            try:
                os.ftruncate(out, 0)
                digest, size = hashlib.sha256(), 0
                while chunk := os.read(fd, 1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES or size + total > MAX_TOTAL_BYTES:
                        raise invalid()
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(out, view) :]
                os.fsync(out)
                os.fchmod(out, 0o400)
            finally:
                os.close(out)
            after = os.fstat(fd)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or digest.hexdigest() != item["sha256"]:
                raise invalid()
            total += size
            metadata = _metadata(target, language)
            if not NAME.fullmatch(str(metadata["name"])) or not VERSION.fullmatch(
                str(metadata["version"])
            ):
                raise invalid()
            if any(
                normalize(p["metadata"]["name"]) == normalize(metadata["name"])
                for p in prepared
            ):
                raise invalid()
            prepared.append(
                {
                    "path": str(target),
                    "name": source.name,
                    "sha256": digest.hexdigest(),
                    "bytes": size,
                    "metadata": metadata,
                }
            )
        finally:
            os.close(fd)
    wanted = {normalize(c["package"]): c["version"] for c in operation["checks"]}
    if any(
        wanted.get(normalize(p["metadata"]["name"])) != p["metadata"]["version"]
        for p in prepared
    ):
        raise CoordinationError("environment_maintenance_invalid_plan")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return prepared


def verify_artifacts(prepared):
    for item in prepared:
        fd = os.open(item["path"], os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != item["bytes"]:
                raise invalid()
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise invalid()
        finally:
            os.close(fd)
