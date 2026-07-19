"""Atomic ownership gate for fresh Skill and composite output directories."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from omicsclaw.common.output_claim import (
    OUTPUT_CLAIM_FILENAME,
    OutputClaimIdentity,
    collect_output_claim_identities,
    first_filesystem_alias_component,
    is_contained_output_path,
    is_filesystem_alias,
    is_output_claim_artifact,
    is_output_claim_path,
    is_scientific_output_file,
    stat_is_filesystem_alias,
)
from ..result import SkillRunAuditIdentity


class OutputDirectoryClaimError(RuntimeError):
    """Raised when a run cannot exclusively claim a fresh output directory."""


_CLAIM_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


def _fresh_directory_error(path: Path, detail: str) -> OutputDirectoryClaimError:
    return OutputDirectoryClaimError(
        f"Refusing output directory '{path}': {detail}; "
        "choose a fresh output directory."
    )


def claim_fresh_output_directory(
    path: str | Path,
    *,
    owner: str,
) -> Path:
    """Atomically claim an empty output directory and leave a durable marker.

    The directory may be absent or already empty (for compatibility with the
    project run resolver), but it must contain no prior artifacts.  The hidden
    marker uses ``O_EXCL`` so two concurrent runs cannot both adopt the same
    target.  It intentionally survives completion/crash: any later reuse must
    fail rather than letting stale ``result.json`` or scientific artifacts
    influence a new execution.
    """
    candidate = Path(path).expanduser()
    try:
        alias = first_filesystem_alias_component(candidate)
    except (OSError, RuntimeError) as exc:
        raise _fresh_directory_error(
            candidate,
            f"the target path cannot be inspected ({exc})",
        ) from exc
    if alias is not None:
        raise _fresh_directory_error(
            candidate,
            f"the target path contains a symbolic link or reparse point ({alias})",
        )
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fresh_directory_error(candidate, f"the target cannot be created ({exc})") from exc
    if is_filesystem_alias(candidate) or not candidate.is_dir():
        raise _fresh_directory_error(candidate, "the target is not a regular directory")

    directory = candidate.resolve()
    try:
        existing = list(directory.iterdir())
    except OSError as exc:
        raise _fresh_directory_error(directory, f"the target cannot be inspected ({exc})") from exc
    if existing:
        raise _fresh_directory_error(directory, "the target already contains artifacts")

    claim_path = directory / OUTPUT_CLAIM_FILENAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(claim_path, flags, 0o600)
    except FileExistsError as exc:
        raise _fresh_directory_error(directory, "the target is already claimed") from exc
    except OSError as exc:
        raise _fresh_directory_error(directory, f"the target cannot be claimed ({exc})") from exc

    claim = {
        "schema_version": 1,
        "claim_id": uuid4().hex,
        "owner": str(owner)[:128],
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(claim, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        extras = [entry for entry in directory.iterdir() if entry != claim_path]
        if extras:
            try:
                claim_path.unlink()
            except OSError:
                pass
            raise _fresh_directory_error(
                directory,
                "artifacts appeared while the target was being claimed",
            )
        _fsync_directory(directory)
    except OutputDirectoryClaimError:
        raise
    except OSError as exc:
        try:
            claim_path.unlink()
        except OSError:
            pass
        raise _fresh_directory_error(directory, f"the claim could not be persisted ({exc})") from exc
    return directory


def bind_output_claim_audit_identity(
    path: str | Path,
    *,
    owner: str,
    audit_identity: SkillRunAuditIdentity,
    runtime_source: str,
) -> Path:
    """Atomically bind the selected runtime identity to an existing claim.

    The initial exclusive claim establishes ownership before runtime
    resolution.  Once the shared runner has selected the actual interpreter
    and frozen its manifest/source hashes, this second phase replaces only the
    owned regular claim file.  Alias/reparse and hard-link checks deliberately
    mirror the fresh-directory gate so evidence cannot be redirected outside
    the Run directory.
    """

    directory = Path(path).expanduser()
    claim_path = directory / OUTPUT_CLAIM_FILENAME
    try:
        alias = first_filesystem_alias_component(claim_path)
    except (OSError, RuntimeError) as exc:
        raise _fresh_directory_error(
            directory,
            f"the run claim cannot be inspected ({exc})",
        ) from exc
    if alias is not None:
        raise _fresh_directory_error(
            directory,
            f"the run claim contains a symbolic link or reparse point ({alias})",
        )
    try:
        directory_stat = os.lstat(directory)
    except OSError as exc:
        raise _fresh_directory_error(
            directory,
            f"the claimed directory cannot be inspected ({exc})",
        ) from exc
    if stat_is_filesystem_alias(directory_stat) or not stat.S_ISDIR(
        directory_stat.st_mode
    ):
        raise _fresh_directory_error(
            directory,
            "the claimed output is not a plain directory",
        )

    if (
        not isinstance(audit_identity, SkillRunAuditIdentity)
        or not isinstance(owner, str)
        or not owner
    ):
        raise _fresh_directory_error(directory, "the audit binding is invalid")
    normalized_runtime_source = str(runtime_source).strip()
    if (
        not normalized_runtime_source
        or normalized_runtime_source == "unknown"
        or len(normalized_runtime_source) > 256
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized_runtime_source
        )
    ):
        raise _fresh_directory_error(directory, "the runtime source is invalid")

    payload, original_identity = _read_owned_claim(claim_path)
    if payload.get("schema_version") != 1 or payload.get("owner") != owner:
        raise _fresh_directory_error(
            directory,
            "the run claim owner does not match the prepared Skill",
        )
    if not isinstance(payload.get("claim_id"), str) or not _CLAIM_ID_RE.fullmatch(
        payload["claim_id"]
    ):
        raise _fresh_directory_error(directory, "the run claim identity is invalid")

    audit_payload = audit_identity.to_dict()
    existing_audit = payload.get("audit_identity")
    existing_runtime = payload.get("runtime_source")
    if existing_audit is not None or existing_runtime is not None:
        if existing_audit == audit_payload and existing_runtime == normalized_runtime_source:
            return directory
        raise _fresh_directory_error(
            directory,
            "the run claim is already bound to different execution evidence",
        )

    updated = dict(payload)
    updated["audit_identity"] = audit_payload
    updated["runtime_source"] = normalized_runtime_source
    serialized = (
        json.dumps(updated, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory,
            prefix=f".{OUTPUT_CLAIM_FILENAME}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())

        temporary_stat = os.lstat(temporary_path)
        if (
            stat_is_filesystem_alias(temporary_stat)
            or not stat.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_nlink != 1
        ):
            raise OSError("temporary claim is not a regular single-link file")
        observed_claim = os.lstat(claim_path)
        if (
            stat_is_filesystem_alias(observed_claim)
            or not stat.S_ISREG(observed_claim.st_mode)
            or observed_claim.st_nlink != 1
            or (observed_claim.st_dev, observed_claim.st_ino) != original_identity
        ):
            raise OSError("run claim changed during audit binding")
        if first_filesystem_alias_component(claim_path) is not None:
            raise OSError("run claim path became a filesystem alias")
        os.replace(temporary_path, claim_path)
        temporary_path = None
        rebound, _ = _read_owned_claim(claim_path)
        if rebound != updated:
            raise OSError("persisted audit binding could not be verified")
        _fsync_directory(directory)
    except (OSError, RuntimeError) as exc:
        raise _fresh_directory_error(
            directory,
            f"the audit binding could not be persisted ({exc})",
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return directory


def _read_owned_claim(path: Path) -> tuple[dict[str, Any], tuple[int, int]]:
    """Read one bounded regular single-link claim without following aliases."""

    before = os.lstat(path)
    if (
        stat_is_filesystem_alias(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise OSError("run claim is not a regular single-link file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or identity != (before.st_dev, before.st_ino)
        ):
            raise OSError("run claim changed while opening")
        chunks: list[bytes] = []
        remaining = 64 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > 64 * 1024:
        raise OSError("run claim exceeds the size limit")
    after = os.lstat(path)
    if after.st_nlink != 1 or (after.st_dev, after.st_ino) != identity:
        raise OSError("run claim changed while reading")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("run claim is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise OSError("run claim must be a JSON object")
    return decoded, identity


def _fsync_directory(path: Path) -> None:
    """Make the atomic claim replacement durable where directory fsync exists."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":  # Windows does not expose portable directory fsync.
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(descriptor)


__all__ = [
    "OUTPUT_CLAIM_FILENAME",
    "OutputClaimIdentity",
    "OutputDirectoryClaimError",
    "bind_output_claim_audit_identity",
    "claim_fresh_output_directory",
    "collect_output_claim_identities",
    "is_contained_output_path",
    "is_output_claim_artifact",
    "is_output_claim_path",
    "is_scientific_output_file",
]
