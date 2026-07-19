"""Filesystem ownership primitives for AutoAgent session and trial outputs."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from omicsclaw.common.output_claim import (
    OUTPUT_CLAIM_FILENAME,
    stat_is_filesystem_alias,
    is_scientific_output_file,
)
from omicsclaw.common.report import SCAFFOLD_STATUS, validate_result_envelope
from omicsclaw.skill.result import SkillRunAuditIdentity


_SESSION_CLAIM_FILE = ".omicsclaw-autoagent-session"
_CLAIM_ID_RE = re.compile(r"[0-9a-f]{32}")
_REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class VerifiedChildTrialReceipt:
    """One read-stable child receipt verified against frozen Skill authority."""

    output_dir: Path
    canonical_skill_id: str
    skill_version: str
    manifest_hash: str
    source_hash: str
    environment_id: str
    runtime_source: str
    claim_id: str
    claim_identity: tuple[int, int]
    claim_sha256: str
    result_sha256: str
    result_payload: dict[str, Any]

    def to_audit_dict(self) -> dict[str, Any]:
        """Return privacy-minimal durable evidence for ledger/trace binding."""

        return {
            "schema_version": 1,
            "canonical_skill_id": self.canonical_skill_id,
            "skill_version": self.skill_version,
            "manifest_hash": self.manifest_hash,
            "source_hash": self.source_hash,
            "environment_id": self.environment_id,
            "runtime_source": self.runtime_source,
            "claim_id": self.claim_id,
            "claim_identity": {
                "device": self.claim_identity[0],
                "inode": self.claim_identity[1],
            },
            "claim_sha256": self.claim_sha256,
            "result_sha256": self.result_sha256,
        }


def canonical_output_path(path: str | Path) -> Path:
    """Return one absolute lexical path after preserving alias evidence."""

    raw = _inspect_raw_path(path)
    return Path(os.path.normpath(os.fspath(raw)))


def claim_session_output_root(path: str | Path) -> Path:
    """Atomically claim a new directory for one AutoAgent session.

    Existing directories are never adopted, even when empty.  The directory
    itself is the atomic ownership claim; a hidden marker records that claim.
    """

    return _create_session_output_root_claim(path, claim_id=uuid4().hex)


def preclaim_session_output_root(path: str | Path, *, claim_id: str) -> Path:
    """Claim the exact future child output root with Backend authority."""

    if not isinstance(claim_id, str) or _CLAIM_ID_RE.fullmatch(claim_id) is None:
        raise ValueError("AutoAgent session output claim ID is invalid")
    return _create_session_output_root_claim(path, claim_id=claim_id)


def _create_session_output_root_claim(
    path: str | Path,
    *,
    claim_id: str,
) -> Path:
    root = canonical_output_path(path)
    if os.path.lexists(root):
        raise ValueError(f"AutoAgent output root already exists: {root}")

    _create_plain_parent_directories(root.parent)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError(f"AutoAgent output root already exists: {root}") from exc
    except OSError as exc:
        raise ValueError(f"AutoAgent output root cannot be created: {root}") from exc

    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise ValueError(f"AutoAgent output root cannot be inspected: {root}") from exc
    if stat_is_filesystem_alias(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError(f"AutoAgent output root is not a plain directory: {root}")

    claim_path = root / _SESSION_CLAIM_FILE
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(claim_path, flags, 0o600)
    except OSError as exc:
        raise ValueError(f"AutoAgent output root is already claimed: {root}") from exc
    claim = {
        "schema_version": 1,
        "claim_id": claim_id,
        "owner": "autoagent-session",
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(claim, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            claim_path.unlink()
        except OSError:
            pass
        raise ValueError(
            f"AutoAgent output root claim could not be persisted: {root}"
        ) from exc
    return root


def adopt_preclaimed_session_output_root(
    path: str | Path,
    *,
    claim_id: str,
) -> Path:
    """Adopt one exact Backend-created claim without creating a second owner."""

    if not isinstance(claim_id, str) or _CLAIM_ID_RE.fullmatch(claim_id) is None:
        raise ValueError("AutoAgent session output claim ID is invalid")
    root = canonical_output_path(path)
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise ValueError("Preclaimed AutoAgent output root does not exist") from exc
    if stat_is_filesystem_alias(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("Preclaimed AutoAgent output root is not a plain directory")
    try:
        names = sorted(entry.name for entry in os.scandir(root))
    except OSError as exc:
        raise ValueError("Preclaimed AutoAgent output root cannot be read") from exc
    if names != [_SESSION_CLAIM_FILE]:
        raise ValueError("Preclaimed AutoAgent output root is not pristine")
    payload, _identity, _digest = _read_single_link_json(
        root / _SESSION_CLAIM_FILE,
        label="AutoAgent session claim",
        max_bytes=64 * 1024,
    )
    if (
        set(payload) != {"schema_version", "claim_id", "owner", "claimed_at"}
        or payload.get("schema_version") != 1
        or payload.get("claim_id") != claim_id
        or payload.get("owner") != "autoagent-session"
        or not _is_timezone_aware_timestamp(payload.get("claimed_at"))
    ):
        raise ValueError("Preclaimed AutoAgent output authority is invalid")
    return root


def bind_unclaimed_trial_output(path: str | Path) -> Path:
    """Bind a trial to one canonical path that no prior run has claimed."""

    canonical = canonical_output_path(path)
    if os.path.lexists(canonical):
        raise ValueError(f"AutoAgent trial output already exists: {canonical}")
    return canonical


def verify_child_trial_receipt(
    output_dir: str | Path,
    *,
    canonical_skill_id: str,
    skill_version: str,
    manifest_hash: str,
    source_hash: str,
) -> VerifiedChildTrialReceipt:
    """Verify and return a read-stable child claim/result snapshot."""

    root = canonical_output_path(output_dir)
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise ValueError("exact output directory was not created") from exc
    if stat_is_filesystem_alias(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("exact output path is not a plain directory")

    claim_payload, claim_identity, claim_sha256 = _read_single_link_json(
        root / OUTPUT_CLAIM_FILENAME,
        label="run claim",
        max_bytes=64 * 1024,
    )
    expected_owner = f"skill:{canonical_skill_id}"
    claim_id = claim_payload.get("claim_id")
    claimed_at = claim_payload.get("claimed_at")
    if (
        claim_payload.get("schema_version") != 1
        or claim_payload.get("owner") != expected_owner
        or not isinstance(claim_id, str)
        or not _CLAIM_ID_RE.fullmatch(claim_id)
        or not _is_timezone_aware_timestamp(claimed_at)
    ):
        raise ValueError(
            f"run claim does not identify the executed Skill {canonical_skill_id!r}"
        )

    if not _REVISION_RE.fullmatch(manifest_hash) or not _REVISION_RE.fullmatch(
        source_hash
    ):
        raise ValueError("frozen trial manifest/source authority is invalid")
    raw_audit_identity = claim_payload.get("audit_identity")
    if not isinstance(raw_audit_identity, dict):
        raise ValueError("run claim has no bound execution audit identity")
    if set(raw_audit_identity) != {
        "skill_id",
        "skill_version",
        "skill_hash",
        "source_hash",
        "environment_id",
    }:
        raise ValueError("run claim execution audit identity is invalid")
    try:
        claim_audit_identity = SkillRunAuditIdentity(
            skill_id=raw_audit_identity["skill_id"],
            skill_version=raw_audit_identity["skill_version"],
            skill_hash=raw_audit_identity["skill_hash"],
            source_hash=raw_audit_identity["source_hash"],
            environment_id=raw_audit_identity["environment_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run claim execution audit identity is invalid") from exc
    if (
        claim_audit_identity.skill_id != canonical_skill_id
        or claim_audit_identity.skill_version != skill_version
        or claim_audit_identity.skill_hash != manifest_hash
        or claim_audit_identity.source_hash != source_hash
    ):
        raise ValueError(
            "run claim execution audit identity does not match trial authority"
        )
    if claim_audit_identity.environment_id == "unknown":
        raise ValueError("run claim execution environment is unknown")
    runtime_source = claim_payload.get("runtime_source")
    if (
        not isinstance(runtime_source, str)
        or not runtime_source
        or runtime_source == "unknown"
        or runtime_source != runtime_source.strip()
        or len(runtime_source) > 256
        or any(
            ord(character) < 32 or ord(character) == 127 for character in runtime_source
        )
    ):
        raise ValueError("run claim runtime source is missing or invalid")

    result_path = root / "result.json"
    if not is_scientific_output_file(
        result_path,
        output_root=root,
        claim_identities=frozenset({claim_identity}),
    ):
        raise ValueError("owned result.json was not produced")
    result_payload, _result_identity, result_sha256 = _read_single_link_json(
        result_path,
        label="result.json",
        max_bytes=8 * 1024 * 1024,
    )
    problems = validate_result_envelope(result_payload)
    if problems:
        raise ValueError("invalid result envelope: " + "; ".join(problems))
    if result_payload.get("status") in {SCAFFOLD_STATUS, "failed"}:
        raise ValueError("failed or scaffold result is not a completed trial")
    if result_payload.get("skill") != canonical_skill_id:
        raise ValueError("result envelope Skill does not match trial authority")
    if result_payload.get("version") != skill_version:
        raise ValueError("result envelope version does not match trial authority")
    return VerifiedChildTrialReceipt(
        output_dir=root,
        canonical_skill_id=canonical_skill_id,
        skill_version=skill_version,
        manifest_hash=manifest_hash,
        source_hash=source_hash,
        environment_id=claim_audit_identity.environment_id,
        runtime_source=runtime_source,
        claim_id=claim_id,
        claim_identity=claim_identity,
        claim_sha256=claim_sha256,
        result_sha256=result_sha256,
        result_payload=result_payload,
    )


def _inspect_raw_path(path: str | Path) -> Path:
    """Inspect every raw component before any normalisation or mutation."""

    candidate = Path(path).expanduser()
    raw = candidate if candidate.is_absolute() else Path.cwd() / candidate
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        if part == "..":
            current = current.parent
            continue
        current /= part
        try:
            component_stat = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                f"AutoAgent output path cannot be inspected: {current}"
            ) from exc
        if stat_is_filesystem_alias(component_stat):
            raise ValueError(
                f"AutoAgent output path contains a symbolic link: {current}"
            )
    return raw


def _create_plain_parent_directories(parent: Path) -> None:
    """Create missing parents one component at a time without alias adoption."""

    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current /= part
        try:
            component_stat = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise ValueError(
                    f"AutoAgent output parent cannot be created: {current}"
                ) from exc
            try:
                component_stat = os.lstat(current)
            except OSError as exc:
                raise ValueError(
                    f"AutoAgent output parent cannot be inspected: {current}"
                ) from exc
        except OSError as exc:
            raise ValueError(
                f"AutoAgent output parent cannot be inspected: {current}"
            ) from exc

        if stat_is_filesystem_alias(component_stat):
            raise ValueError(
                f"AutoAgent output parent contains a symbolic link: {current}"
            )
        if not stat.S_ISDIR(component_stat.st_mode):
            raise ValueError(f"AutoAgent output parent is not a directory: {current}")


def _read_single_link_json(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> tuple[dict[str, object], tuple[int, int], str]:
    """Read bounded JSON through a no-follow descriptor tied to one inode."""

    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} is missing") from exc
    if stat_is_filesystem_alias(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} is not a regular file")
    if before.st_nlink != 1:
        raise ValueError(f"{label} is multiply linked")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"{label} changed while opening")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > max_bytes:
        raise ValueError(f"{label} exceeds the size limit")

    try:
        after = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} changed while reading") from exc
    if after.st_nlink != 1 or (after.st_dev, after.st_ino) != (
        before.st_dev,
        before.st_ino,
    ):
        raise ValueError(f"{label} changed while reading")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return (
        decoded,
        (opened.st_dev, opened.st_ino),
        "sha256:" + hashlib.sha256(payload).hexdigest(),
    )


def _is_timezone_aware_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None
