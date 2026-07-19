"""Filesystem Adapter for canonical Run Manifest and artifact evidence.

The Control Database keeps only an opaque ``manifest_ref``.  This Module owns
the mapping from that reference to one scoped Run root, keeps the immutable
header separate from the shared Skill runner's fresh ``artifacts/`` leaf, and
publishes terminal evidence only after re-verifying every owned artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import threading
import time
from typing import Any, Mapping

from omicsclaw.common.output_claim import (
    OUTPUT_CLAIM_FILENAME,
    atomic_write_owned_output_text,
    collect_output_claim_identities,
    first_filesystem_alias_component,
    is_contained_output_path,
    is_output_claim_artifact,
    is_scientific_output_file,
    stat_is_filesystem_alias,
)
from omicsclaw.common.report import (
    SCAFFOLD_STATUS,
    load_result_json,
    validate_result_envelope,
)
from omicsclaw.common.run_paths import resolve_project_dir
from omicsclaw.skill.result import SkillRunAuditIdentity, SkillRunResult

from .run_contract import RunScope, canonical_json_bytes


_MANIFEST_FILENAME = "manifest.json"
_MAX_STORED_JSON_BYTES = 4 * 1024 * 1024
_REFERENCE_RE = re.compile(r"run-store:v1:([0-9a-f]{32})\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SKILL_DIGEST_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_MAX_ARTIFACT_READ_CHUNK_BYTES = 1024 * 1024
_REVISION_KEYS = frozenset(
    {"skill_id", "skill_version", "manifest_hash", "source_hash"}
)


class RunStoreIntegrityError(RuntimeError):
    """Raised when Run Store identity or immutable evidence cannot be proven."""


def _canonical_store_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Canonical spelling for bounded Store records, including inventories."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Run Store record must be canonical JSON") from exc
    encoded = rendered.encode("utf-8")
    if len(encoded) > _MAX_STORED_JSON_BYTES:
        raise ValueError("Run Store record exceeds the storage size limit")
    return encoded


@dataclass(frozen=True, slots=True)
class RunManifestHeader:
    run_id: str
    run_submission_id: str
    fingerprint_version: int
    fingerprint_sha256: str
    run_kind: str
    scope: RunScope
    inputs: Mapping[str, Any]
    parameters: Mapping[str, Any]
    resource_contract: Mapping[str, Any]
    skill_revision: Mapping[str, str]

    def __post_init__(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("run_submission_id", self.run_submission_id),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
                raise ValueError(f"{label} must be an opaque 32-hex identifier")
        if (
            not isinstance(self.fingerprint_version, int)
            or isinstance(self.fingerprint_version, bool)
            or self.fingerprint_version < 1
        ):
            raise ValueError("fingerprint_version must be positive")
        if not isinstance(self.fingerprint_sha256, str) or not _DIGEST_RE.fullmatch(
            self.fingerprint_sha256
        ):
            raise ValueError("fingerprint_sha256 must be a SHA-256 digest")
        if self.run_kind != "skill":
            raise ValueError("the V1 Run Store tracer supports only Skill Runs")
        revision = dict(self.skill_revision)
        if set(revision) != _REVISION_KEYS:
            raise ValueError("skill_revision must contain the frozen revision fields")
        if revision["skill_id"] != self.inputs.get("skill_id"):
            raise ValueError("skill revision does not match the requested Skill")
        for key in ("manifest_hash", "source_hash"):
            if not _SKILL_DIGEST_RE.fullmatch(str(revision[key])):
                raise ValueError(f"skill_revision {key} must be a SHA-256 digest")
        # Validate JSON shape and bounded size before any filesystem mutation.
        canonical_json_bytes(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_submission_id": self.run_submission_id,
            "fingerprint_version": self.fingerprint_version,
            "fingerprint_sha256": self.fingerprint_sha256,
            "run_kind": self.run_kind,
            "scope": self.scope.to_dict(),
            "inputs": _json_copy(self.inputs),
            "parameters": _json_copy(self.parameters),
            "resource_contract": _json_copy(self.resource_contract),
            "skill_revision": {
                str(key): str(value) for key, value in self.skill_revision.items()
            },
        }


@dataclass(frozen=True, slots=True)
class ProvisionalRunManifest:
    manifest_ref: str
    artifacts_dir: Path


@dataclass(frozen=True, slots=True)
class RunCompletionEvidence:
    manifest_ref: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class RunHeaderProjection:
    """Minimal verified Manifest header view safe for Runtime adapters."""

    skill_id: str


@dataclass(frozen=True, slots=True)
class VerifiedRunOutput:
    """Verified local output paths for the CLI compatibility Adapter."""

    skill_id: str
    output_dir: str
    readme_path: str | None = None
    notebook_path: str | None = None


@dataclass(frozen=True, slots=True)
class RunStoreTerminalProjection:
    """Pure verified terminal view; Manifest dictionaries never cross this Seam."""

    skill_id: str
    output: VerifiedRunOutput | None


@dataclass(frozen=True, slots=True)
class VerifiedRunArtifact:
    """One deeply verified, path-free artifact inventory item."""

    relative_path: str
    size_bytes: int
    sha256: str
    media_type: str


@dataclass(frozen=True, slots=True)
class VerifiedRunArtifactInventory:
    """Immutable successful-Run artifact projection for Runtime adapters."""

    skill_id: str
    artifacts: tuple[VerifiedRunArtifact, ...]


class VerifiedRunArtifactFile:
    """Own one already-open, verified file descriptor until explicit close.

    Reads use ``pread`` against the descriptor that was hashed, so callers
    never reopen a path after verification.  Each chunk also fences against
    in-place mutation by comparing the complete file identity before and after
    the read.
    """

    def __init__(
        self,
        *,
        descriptor: int,
        artifact: VerifiedRunArtifact,
        identity: tuple[int, int, int, int, int, int, int],
    ) -> None:
        self.artifact = artifact
        self._descriptor = descriptor
        self._identity = identity
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def read_chunk(
        self,
        *,
        offset: int,
        max_bytes: int = _MAX_ARTIFACT_READ_CHUNK_BYTES,
    ) -> bytes:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("artifact read offset must be a non-negative integer")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= _MAX_ARTIFACT_READ_CHUNK_BYTES
        ):
            raise ValueError("artifact read chunk exceeds the bounded size")
        if offset >= self.artifact.size_bytes:
            return b""
        expected_length = min(max_bytes, self.artifact.size_bytes - offset)
        with self._lock:
            if self._closed:
                raise RunStoreIntegrityError("verified artifact reader is closed")
            before = _descriptor_identity(self._descriptor)
            if before != self._identity:
                raise RunStoreIntegrityError("verified artifact changed before read")
            try:
                data = os.pread(self._descriptor, expected_length, offset)
            except OSError as exc:
                raise RunStoreIntegrityError("verified artifact cannot be read") from exc
            after = _descriptor_identity(self._descriptor)
            if after != self._identity or len(data) != expected_length:
                raise RunStoreIntegrityError("verified artifact changed while reading")
            return data

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            descriptor = self._descriptor
            self._descriptor = -1
        try:
            os.close(descriptor)
        except OSError as exc:
            raise RunStoreIntegrityError("verified artifact reader close failed") from exc

    def __enter__(self) -> "VerifiedRunArtifactFile":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class FilesystemRunStore:
    """Durable local Run Store with opaque reference indirection."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).expanduser().absolute()
        if first_filesystem_alias_component(self.output_root) is not None:
            raise RunStoreIntegrityError("Run Store output root crosses an alias")
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._require_owned_directory(self.output_root)
        internal_root = self.output_root / ".run-store"
        self._ensure_owned_directory(internal_root)
        self._reference_root = internal_root / "refs"
        self._ensure_owned_directory(self._reference_root)

    def create_header(
        self,
        header: RunManifestHeader,
        *,
        project_name: str = "",
    ) -> ProvisionalRunManifest:
        """Publish and verify one provisional immutable Manifest header."""

        if not isinstance(header, RunManifestHeader):
            raise TypeError("header must be RunManifestHeader")
        token = secrets.token_hex(16)
        manifest_ref = f"run-store:v1:{token}"
        if header.scope.kind == "project":
            assert header.scope.project_id is not None
            parent = resolve_project_dir(
                self.output_root,
                header.scope.project_id,
                project_name,
                create=True,
            )
        else:
            parent = self.output_root / "default"
            self._ensure_owned_directory(parent)
        self._require_owned_directory(parent)
        skill = str(header.inputs["skill_id"])
        run_root = parent / f"{skill}__{header.run_id[:10]}"
        try:
            run_root.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise RunStoreIntegrityError(
                "canonical Run directory already exists"
            ) from exc

        relative_root = run_root.relative_to(self.output_root).as_posix()
        header_payload = header.to_dict()
        header_sha256 = hashlib.sha256(canonical_json_bytes(header_payload)).hexdigest()
        manifest = {
            "schema_version": 1,
            "header": header_payload,
            "acceptance": {"state": "provisional"},
            "completion": None,
        }
        try:
            self._write_manifest(run_root, manifest)
            reference = {
                "schema_version": 1,
                "token": token,
                "relative_root": relative_root,
                "run_id": header.run_id,
                "header_sha256": header_sha256,
            }
            self._atomic_json(
                self._reference_path(token),
                reference,
                label="Run Store reference",
            )
            observed = self.read_manifest(manifest_ref)
            if observed != manifest:
                raise RunStoreIntegrityError("Run Manifest header verification failed")
        except BaseException:
            self._remove_provisional_paths(token, run_root)
            raise
        return ProvisionalRunManifest(manifest_ref, run_root / "artifacts")

    def mark_accepted(self, manifest_ref: str) -> None:
        run_root, manifest = self._load(manifest_ref)
        state = manifest.get("acceptance", {}).get("state")
        if state == "accepted":
            return
        if state != "provisional" or manifest.get("completion") is not None:
            raise RunStoreIntegrityError("Run Manifest cannot enter accepted state")
        updated = dict(manifest)
        updated["acceptance"] = {"state": "accepted"}
        self._write_manifest(run_root, updated)
        if self.read_manifest(manifest_ref) != updated:
            raise RunStoreIntegrityError("accepted Run Manifest verification failed")

    def abandon(self, manifest_ref: str) -> None:
        token = self._token(manifest_ref)
        run_root, manifest = self._load(manifest_ref)
        if manifest.get("acceptance", {}).get("state") != "provisional":
            raise RunStoreIntegrityError("accepted Run Manifest cannot be abandoned")
        if manifest.get("completion") is not None:
            raise RunStoreIntegrityError("completed Run Manifest cannot be abandoned")
        self._remove_provisional_paths(token, run_root)

    def artifacts_dir(self, manifest_ref: str) -> Path:
        run_root, _ = self._load(manifest_ref)
        return run_root / "artifacts"

    def execution_tmp_dir(self, manifest_ref: str) -> Path:
        """Return the Run-owned temporary directory outside artifact inventory."""

        run_root, _ = self._load(manifest_ref)
        temporary = run_root / ".tmp"
        if temporary.exists():
            try:
                details = os.lstat(temporary)
            except OSError as exc:
                raise RunStoreIntegrityError(
                    "Run temporary directory is unsafe"
                ) from exc
            if stat_is_filesystem_alias(details) or not stat.S_ISDIR(details.st_mode):
                raise RunStoreIntegrityError("Run temporary directory is unsafe")
        else:
            temporary.mkdir(mode=0o700)
        return temporary

    def read_manifest(self, manifest_ref: str) -> dict[str, Any]:
        _, manifest = self._load(manifest_ref)
        return manifest

    def verify_receipt_binding(
        self,
        manifest_ref: str,
        *,
        run_id: str,
        run_kind: str,
        scope_kind: str,
        project_id: str | None,
    ) -> dict[str, Any]:
        """Verify one Manifest header against its authoritative Run Receipt."""

        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("run_id must be an opaque 32-hex identifier")
        if run_kind != "skill":
            raise ValueError("the V1 Run Store tracer supports only Skill Runs")
        if scope_kind == "project":
            if not isinstance(project_id, str) or not re.fullmatch(
                r"[0-9a-f]{32}", project_id
            ):
                raise ValueError("Project Run Receipt requires an opaque Project ID")
            expected_scope = {"kind": "project", "project_id": project_id}
        elif scope_kind == "unassigned":
            if project_id is not None:
                raise ValueError("Unassigned Run Receipt cannot carry a Project ID")
            expected_scope = {"kind": "unassigned"}
        else:
            raise ValueError("Run Receipt has an unsupported Scope")
        manifest = self.read_manifest(manifest_ref)
        header = manifest.get("header")
        if (
            not isinstance(header, dict)
            or header.get("run_id") != run_id
            or header.get("run_kind") != run_kind
            or header.get("scope") != expected_scope
            or manifest.get("acceptance") != {"state": "accepted"}
        ):
            raise RunStoreIntegrityError("Run Manifest and Receipt binding mismatch")
        return manifest

    def project_receipt_header(
        self,
        manifest_ref: str,
        *,
        run_id: str,
        run_kind: str,
        scope_kind: str,
        project_id: str | None,
    ) -> RunHeaderProjection:
        """Return only the Skill identity after deep Receipt binding proof."""

        manifest = self.verify_receipt_binding(
            manifest_ref,
            run_id=run_id,
            run_kind=run_kind,
            scope_kind=scope_kind,
            project_id=project_id,
        )
        return self._project_header(manifest)

    @staticmethod
    def _project_header(manifest: Mapping[str, Any]) -> RunHeaderProjection:
        header = manifest.get("header")
        inputs = header.get("inputs") if isinstance(header, Mapping) else None
        skill_id = inputs.get("skill_id") if isinstance(inputs, Mapping) else None
        if (
            not isinstance(skill_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", skill_id)
        ):
            raise RunStoreIntegrityError("Run Manifest Skill identity is invalid")
        return RunHeaderProjection(skill_id=skill_id)

    def project_verified_terminal(
        self,
        manifest_ref: str,
        *,
        run_id: str,
        run_kind: str,
        scope_kind: str,
        project_id: str | None,
        assignment_id: str | None,
        terminal_status: str,
        terminal_code: str | None,
    ) -> RunStoreTerminalProjection:
        """Verify Receipt binding and terminal evidence without writing state."""

        if terminal_status not in {"succeeded", "failed", "canceled", "interrupted"}:
            raise ValueError("Run terminal status is invalid")
        manifest = self.verify_receipt_binding(
            manifest_ref,
            run_id=run_id,
            run_kind=run_kind,
            scope_kind=scope_kind,
            project_id=project_id,
        )
        header = self._project_header(manifest)
        completion = manifest.get("completion")
        if assignment_id is None:
            if terminal_status == "succeeded" or completion is not None:
                raise RunStoreIntegrityError(
                    "unassigned terminal Run has contradictory completion evidence"
                )
            return RunStoreTerminalProjection(skill_id=header.skill_id, output=None)
        self._require_assignment_id(assignment_id)
        if not isinstance(completion, Mapping):
            raise RunStoreIntegrityError("assigned terminal Run has no completion")
        if (
            completion.get("assignment_id") != assignment_id
            or completion.get("kind") != terminal_status
        ):
            raise RunStoreIntegrityError("Run terminal completion identity mismatch")
        if terminal_status == "succeeded":
            if terminal_code is not None:
                raise RunStoreIntegrityError("successful Run has a terminal code")
            verified = self.verify_success(
                manifest_ref,
                assignment_id=assignment_id,
            )
            artifacts_dir = self.artifacts_dir(manifest_ref)
            inventory = verified["completion"]["artifacts"]
            readme_path: str | None = None
            notebook_path: str | None = None
            for item in inventory:
                relative = PurePosixPath(str(item["path"]))
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                ):
                    raise RunStoreIntegrityError("Run artifact projection is unsafe")
                rendered = str(artifacts_dir.joinpath(*relative.parts))
                if relative.as_posix() == "README.md":
                    readme_path = rendered
                elif relative.as_posix() == (
                    "reproducibility/analysis_notebook.ipynb"
                ):
                    notebook_path = rendered
            return RunStoreTerminalProjection(
                skill_id=header.skill_id,
                output=VerifiedRunOutput(
                    skill_id=header.skill_id,
                    output_dir=str(artifacts_dir),
                    readme_path=readme_path,
                    notebook_path=notebook_path,
                ),
            )
        if completion.get("terminal_code") != terminal_code:
            raise RunStoreIntegrityError("Run terminal completion code mismatch")
        if terminal_status == "failed":
            evidence = completion.get("execution_evidence")
            if not isinstance(evidence, Mapping):
                raise RunStoreIntegrityError("Run failure evidence is invalid")
            self.verify_failure(
                manifest_ref,
                terminal_code=str(terminal_code),
                execution_evidence=evidence,
                assignment_id=assignment_id,
            )
        else:
            self.verify_stop(
                manifest_ref,
                terminal_status=terminal_status,
                terminal_code=str(terminal_code),
                assignment_id=assignment_id,
            )
        return RunStoreTerminalProjection(skill_id=header.skill_id, output=None)

    def project_verified_artifacts(
        self,
        manifest_ref: str,
        *,
        run_id: str,
        run_kind: str,
        scope_kind: str,
        project_id: str | None,
        assignment_id: str | None,
        terminal_status: str,
        terminal_code: str | None,
    ) -> VerifiedRunArtifactInventory:
        """Deep-verify one successful terminal Run and project its inventory."""

        if terminal_status != "succeeded" or terminal_code is not None:
            raise RunStoreIntegrityError(
                "only a successful terminal Run owns downloadable artifacts"
            )
        if assignment_id is None:
            raise RunStoreIntegrityError("successful Run has no Assignment")
        self._require_assignment_id(assignment_id)
        manifest = self.verify_receipt_binding(
            manifest_ref,
            run_id=run_id,
            run_kind=run_kind,
            scope_kind=scope_kind,
            project_id=project_id,
        )
        header = self._project_header(manifest)
        completion = manifest.get("completion")
        if (
            not isinstance(completion, Mapping)
            or completion.get("kind") != "succeeded"
            or completion.get("assignment_id") != assignment_id
        ):
            raise RunStoreIntegrityError("Run artifact completion identity mismatch")
        verified = self.verify_success(
            manifest_ref,
            assignment_id=assignment_id,
        )
        raw_inventory = verified["completion"]["artifacts"]
        if not isinstance(raw_inventory, list):
            raise RunStoreIntegrityError("Run artifact inventory is invalid")
        artifacts = tuple(
            self._verified_artifact_item(item) for item in raw_inventory
        )
        return VerifiedRunArtifactInventory(
            skill_id=header.skill_id,
            artifacts=artifacts,
        )

    def open_verified_artifact(
        self,
        manifest_ref: str,
        *,
        run_id: str,
        run_kind: str,
        scope_kind: str,
        project_id: str | None,
        assignment_id: str | None,
        terminal_status: str,
        terminal_code: str | None,
        relative_path: str,
    ) -> tuple[str, VerifiedRunArtifactFile]:
        """Return the Skill ID and one verified descriptor-backed reader."""

        inventory = self.project_verified_artifacts(
            manifest_ref,
            run_id=run_id,
            run_kind=run_kind,
            scope_kind=scope_kind,
            project_id=project_id,
            assignment_id=assignment_id,
            terminal_status=terminal_status,
            terminal_code=terminal_code,
        )
        normalized = self._validated_relative_artifact_path(relative_path)
        artifact = next(
            (
                item
                for item in inventory.artifacts
                if item.relative_path == normalized.as_posix()
            ),
            None,
        )
        if artifact is None:
            raise KeyError(normalized.as_posix())
        artifacts_dir = self.artifacts_dir(manifest_ref)
        descriptor = self._open_owned_regular_file(artifacts_dir, normalized)
        try:
            identity = _descriptor_identity(descriptor)
            if identity[3] != artifact.size_bytes:
                raise RunStoreIntegrityError("Run artifact size drifted before open")
            digest = hashlib.sha256()
            offset = 0
            while offset < artifact.size_bytes:
                block = os.pread(
                    descriptor,
                    min(_MAX_ARTIFACT_READ_CHUNK_BYTES, artifact.size_bytes - offset),
                    offset,
                )
                if not block:
                    raise RunStoreIntegrityError(
                        "Run artifact ended before its verified size"
                    )
                digest.update(block)
                offset += len(block)
            after = _descriptor_identity(descriptor)
            if after != identity or digest.hexdigest() != artifact.sha256:
                raise RunStoreIntegrityError("Run artifact drifted while opening")
            return inventory.skill_id, VerifiedRunArtifactFile(
                descriptor=descriptor,
                artifact=artifact,
                identity=identity,
            )
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _verified_artifact_item(item: object) -> VerifiedRunArtifact:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "size_bytes",
            "sha256",
            "media_type",
        }:
            raise RunStoreIntegrityError("Run artifact inventory item is invalid")
        try:
            relative = FilesystemRunStore._validated_relative_artifact_path(
                item.get("path")
            )
        except ValueError as exc:
            raise RunStoreIntegrityError(
                "Run artifact inventory item is invalid"
            ) from exc
        size_bytes = item.get("size_bytes")
        digest = item.get("sha256")
        media_type = item.get("media_type")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(digest, str)
            or _DIGEST_RE.fullmatch(digest) is None
            or not isinstance(media_type, str)
            or not media_type
            or len(media_type) > 255
        ):
            raise RunStoreIntegrityError("Run artifact inventory item is invalid")
        return VerifiedRunArtifact(
            relative_path=relative.as_posix(),
            size_bytes=size_bytes,
            sha256=digest,
            media_type=media_type,
        )

    @staticmethod
    def _validated_relative_artifact_path(value: object) -> PurePosixPath:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise ValueError("artifact path must be a normalized relative POSIX path")
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != value
        ):
            raise ValueError("artifact path must be a normalized relative POSIX path")
        return relative

    def _open_owned_regular_file(
        self,
        artifacts_dir: Path,
        relative: PurePosixPath,
    ) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if not no_follow or not directory_flag or not hasattr(os, "pread"):
            raise RunStoreIntegrityError("safe descriptor artifact reads are unsupported")
        try:
            directory_relative = artifacts_dir.relative_to(self.output_root)
        except ValueError as exc:
            raise RunStoreIntegrityError("Run artifact root escapes its Store") from exc
        directory_parts = (*directory_relative.parts, *relative.parts[:-1])
        directory_descriptor = -1
        try:
            directory_descriptor = os.open(
                self.output_root,
                os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
            )
            for part in directory_parts:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_CLOEXEC | directory_flag | no_follow,
                    dir_fd=directory_descriptor,
                )
                os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | os.O_CLOEXEC | no_follow,
                dir_fd=directory_descriptor,
            )
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                os.close(descriptor)
                raise RunStoreIntegrityError(
                    "Run artifact is not an owned regular file"
                )
            return descriptor
        except RunStoreIntegrityError:
            raise
        except OSError as exc:
            raise RunStoreIntegrityError("Run artifact cannot be opened safely") from exc
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)

    def commit_success(
        self,
        manifest_ref: str,
        result: SkillRunResult,
        *,
        assignment_id: str,
    ) -> RunCompletionEvidence:
        """Commit then re-verify immutable successful completion evidence."""

        self._require_assignment_id(assignment_id)
        run_root, manifest = self._load(manifest_ref)
        self._require_accepted_unfinished(manifest)
        artifacts_dir = run_root / "artifacts"
        header = manifest["header"]
        self._validate_success_result(result, artifacts_dir, header)
        inventory = self._artifact_inventory(artifacts_dir)
        result_item = next(
            (item for item in inventory if item["path"] == "result.json"), None
        )
        if result_item is None:
            raise RunStoreIntegrityError("verified completion has no result.json")
        completion: dict[str, Any] = {
            "kind": "succeeded",
            "committed_at_ms": time.time_ns() // 1_000_000,
            "assignment_id": assignment_id,
            "result_envelope_sha256": result_item["sha256"],
            "audit_identity": result.audit_identity.to_dict(),
            "artifacts": inventory,
        }
        updated = dict(manifest)
        updated["completion"] = completion
        self._write_manifest(run_root, updated)
        self.verify_success(manifest_ref, assignment_id=assignment_id)
        return RunCompletionEvidence(
            manifest_ref=manifest_ref,
            manifest_sha256=self._hash_stable_file(run_root / _MANIFEST_FILENAME),
        )

    def commit_failure(
        self,
        manifest_ref: str,
        *,
        terminal_code: str,
        execution_evidence: Mapping[str, Any] | None = None,
        assignment_id: str,
    ) -> RunCompletionEvidence:
        """Persist sanitized non-success evidence without claiming validation."""

        self._require_assignment_id(assignment_id)
        run_root, manifest = self._load(manifest_ref)
        self._require_accepted_unfinished(manifest)
        if terminal_code not in {
            "completion_commit_failed",
            "executor_failed",
            "spawn_failed",
            "validation_failed",
        }:
            raise ValueError("unsupported failure terminal code")
        evidence = _json_copy(execution_evidence or {})
        canonical_json_bytes(evidence)
        updated = dict(manifest)
        updated["completion"] = {
            "kind": "failed",
            "committed_at_ms": time.time_ns() // 1_000_000,
            "assignment_id": assignment_id,
            "terminal_code": terminal_code,
            "execution_evidence": evidence,
        }
        self._write_manifest(run_root, updated)
        self.verify_failure(
            manifest_ref,
            terminal_code=terminal_code,
            execution_evidence=evidence,
            assignment_id=assignment_id,
        )
        return RunCompletionEvidence(
            manifest_ref=manifest_ref,
            manifest_sha256=self._hash_stable_file(run_root / _MANIFEST_FILENAME),
        )

    def verify_failure(
        self,
        manifest_ref: str,
        *,
        terminal_code: str,
        execution_evidence: Mapping[str, Any],
        assignment_id: str,
    ) -> dict[str, Any]:
        self._require_assignment_id(assignment_id)
        _, manifest = self._load(manifest_ref)
        completion = manifest.get("completion")
        expected_evidence = _json_copy(execution_evidence)
        if not isinstance(completion, dict) or set(completion) != {
            "kind",
            "committed_at_ms",
            "assignment_id",
            "terminal_code",
            "execution_evidence",
        }:
            raise RunStoreIntegrityError("failed completion evidence is malformed")
        if (
            completion.get("kind") != "failed"
            or completion.get("assignment_id") != assignment_id
            or completion.get("terminal_code") != terminal_code
            or completion.get("execution_evidence") != expected_evidence
        ):
            raise RunStoreIntegrityError("failed completion evidence mismatch")
        committed_at_ms = completion.get("committed_at_ms")
        if (
            not isinstance(committed_at_ms, int)
            or isinstance(committed_at_ms, bool)
            or committed_at_ms < 0
        ):
            raise RunStoreIntegrityError("failed completion timestamp is invalid")
        return manifest

    def commit_stop(
        self,
        manifest_ref: str,
        *,
        terminal_status: str,
        terminal_code: str,
        assignment_id: str,
    ) -> RunCompletionEvidence:
        """Persist assignment-bound proof that executor shutdown completed."""

        if terminal_status not in {"canceled", "interrupted"}:
            raise ValueError("unsupported stopped Run status")
        expected_codes = {
            "canceled": {"canceled_by_owner"},
            "interrupted": {
                "control_plane_restarted",
                "execution_interrupted",
            },
        }[terminal_status]
        if terminal_code not in expected_codes:
            raise ValueError("stopped Run terminal code does not match its status")
        self._require_assignment_id(assignment_id)
        run_root, manifest = self._load(manifest_ref)
        self._require_accepted_unfinished(manifest)
        updated = dict(manifest)
        updated["completion"] = {
            "kind": terminal_status,
            "committed_at_ms": time.time_ns() // 1_000_000,
            "assignment_id": assignment_id,
            "terminal_code": terminal_code,
            "process_tree_stopped": True,
        }
        self._write_manifest(run_root, updated)
        self.verify_stop(
            manifest_ref,
            terminal_status=terminal_status,
            terminal_code=terminal_code,
            assignment_id=assignment_id,
        )
        return RunCompletionEvidence(
            manifest_ref=manifest_ref,
            manifest_sha256=self._hash_stable_file(run_root / _MANIFEST_FILENAME),
        )

    def verify_stop(
        self,
        manifest_ref: str,
        *,
        terminal_status: str,
        terminal_code: str,
        assignment_id: str,
    ) -> dict[str, Any]:
        self._require_assignment_id(assignment_id)
        _, manifest = self._load(manifest_ref)
        completion = manifest.get("completion")
        if not isinstance(completion, dict) or set(completion) != {
            "kind",
            "committed_at_ms",
            "assignment_id",
            "terminal_code",
            "process_tree_stopped",
        }:
            raise RunStoreIntegrityError("Run stop completion evidence mismatch")
        if (
            completion.get("kind") != terminal_status
            or completion.get("assignment_id") != assignment_id
            or completion.get("terminal_code") != terminal_code
            or completion.get("process_tree_stopped") is not True
        ):
            raise RunStoreIntegrityError("Run stop completion evidence mismatch")
        committed_at_ms = completion.get("committed_at_ms")
        if (
            not isinstance(committed_at_ms, int)
            or isinstance(committed_at_ms, bool)
            or committed_at_ms < 0
        ):
            raise RunStoreIntegrityError("Run stop evidence timestamp is invalid")
        return manifest

    def verify_success(
        self,
        manifest_ref: str,
        *,
        assignment_id: str,
    ) -> dict[str, Any]:
        self._require_assignment_id(assignment_id)
        run_root, manifest = self._load(manifest_ref)
        completion = manifest.get("completion")
        if not isinstance(completion, dict) or set(completion) != {
            "kind",
            "committed_at_ms",
            "assignment_id",
            "result_envelope_sha256",
            "audit_identity",
            "artifacts",
        }:
            raise RunStoreIntegrityError("Run Manifest completion is not successful")
        if completion.get("kind") != "succeeded":
            raise RunStoreIntegrityError("Run Manifest completion is not successful")
        committed_at_ms = completion.get("committed_at_ms")
        if (
            not isinstance(committed_at_ms, int)
            or isinstance(committed_at_ms, bool)
            or committed_at_ms < 0
        ):
            raise RunStoreIntegrityError("Run completion timestamp is invalid")
        if completion.get("assignment_id") != assignment_id:
            raise RunStoreIntegrityError("Run Manifest Assignment evidence mismatch")
        inventory = self._artifact_inventory(run_root / "artifacts")
        if inventory != completion.get("artifacts"):
            raise RunStoreIntegrityError(
                "Run artifact inventory drifted after completion"
            )
        result_payload = load_result_json(run_root / "artifacts")
        problems = validate_result_envelope(result_payload)
        if problems:
            raise RunStoreIntegrityError(
                "Run result envelope is invalid: " + "; ".join(problems)
            )
        result_item = next(
            (item for item in inventory if item["path"] == "result.json"), None
        )
        if result_item is None or result_item["sha256"] != completion.get(
            "result_envelope_sha256"
        ):
            raise RunStoreIntegrityError("Run result completion digest mismatch")
        assert isinstance(result_payload, dict)
        self._validate_result_payload_identity(result_payload, manifest["header"])
        completion_audit = completion.get("audit_identity")
        if not isinstance(completion_audit, dict):
            raise RunStoreIntegrityError("Run completion audit identity is missing")
        try:
            normalized_audit = SkillRunAuditIdentity(**completion_audit)
        except (TypeError, ValueError) as exc:
            raise RunStoreIntegrityError(
                "Run completion audit identity is invalid"
            ) from exc
        expected_revision = manifest["header"]["skill_revision"]
        if (
            normalized_audit.skill_id != expected_revision["skill_id"]
            or normalized_audit.skill_version != expected_revision["skill_version"]
            or normalized_audit.skill_hash != expected_revision["manifest_hash"]
            or normalized_audit.source_hash != expected_revision["source_hash"]
        ):
            raise RunStoreIntegrityError("Run completion audit identity drifted")
        return manifest

    @staticmethod
    def _require_assignment_id(assignment_id: str) -> None:
        if not isinstance(assignment_id, str) or not re.fullmatch(
            r"[0-9a-f]{32}", assignment_id
        ):
            raise ValueError("assignment_id must be an opaque 32-hex identifier")

    def _validate_success_result(
        self,
        result: SkillRunResult,
        artifacts_dir: Path,
        header: Mapping[str, Any],
    ) -> None:
        if not isinstance(result, SkillRunResult) or not result.success:
            raise RunStoreIntegrityError("shared Skill runner did not verify success")
        try:
            actual_output = Path(str(result.output_dir)).resolve(strict=True)
            expected_output = artifacts_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RunStoreIntegrityError(
                "shared runner output directory is unavailable"
            ) from exc
        if actual_output != expected_output:
            raise RunStoreIntegrityError("shared runner output directory mismatch")
        audit = result.audit_identity
        if audit is None:
            raise RunStoreIntegrityError("shared runner result has no audit identity")
        expected = header["skill_revision"]
        if (
            result.skill != expected["skill_id"]
            or audit.skill_id != expected["skill_id"]
            or audit.skill_version != expected["skill_version"]
            or audit.skill_hash != expected["manifest_hash"]
            or audit.source_hash != expected["source_hash"]
        ):
            raise RunStoreIntegrityError("shared runner frozen revision mismatch")
        payload = load_result_json(artifacts_dir)
        problems = validate_result_envelope(payload)
        if problems:
            raise RunStoreIntegrityError(
                "Run result envelope is invalid: " + "; ".join(problems)
            )
        assert isinstance(payload, dict)
        self._validate_result_payload_identity(payload, header)
        if payload.get("status") in {"failed", SCAFFOLD_STATUS}:
            raise RunStoreIntegrityError("Run result envelope does not prove success")

    @staticmethod
    def _validate_result_payload_identity(
        payload: Mapping[str, Any],
        header: Mapping[str, Any],
    ) -> None:
        expected = header.get("skill_revision")
        if not isinstance(expected, Mapping):
            raise RunStoreIntegrityError("Run Manifest Skill revision is missing")
        if payload.get("skill") != expected.get("skill_id") or payload.get(
            "version"
        ) != expected.get("skill_version"):
            raise RunStoreIntegrityError(
                "Run result identity does not match the frozen Skill revision"
            )

    def _artifact_inventory(self, artifacts_dir: Path) -> list[dict[str, Any]]:
        try:
            root_stat = os.lstat(artifacts_dir)
        except OSError as exc:
            raise RunStoreIntegrityError(
                "Run artifacts directory is unavailable"
            ) from exc
        if stat_is_filesystem_alias(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise RunStoreIntegrityError("Run artifacts root is unsafe")
        claim_identities = collect_output_claim_identities(artifacts_dir)
        inventory: list[dict[str, Any]] = []
        for path in sorted(artifacts_dir.rglob("*"), key=lambda item: item.as_posix()):
            try:
                entry_stat = os.lstat(path)
            except OSError as exc:
                raise RunStoreIntegrityError("Run artifact disappeared") from exc
            if stat_is_filesystem_alias(entry_stat):
                raise RunStoreIntegrityError(f"unsafe artifact alias: {path.name}")
            if stat.S_ISDIR(entry_stat.st_mode):
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise RunStoreIntegrityError(f"unsafe artifact type: {path.name}")
            if path.name == OUTPUT_CLAIM_FILENAME and is_output_claim_artifact(
                path,
                output_root=artifacts_dir,
                claim_identities=claim_identities,
            ):
                continue
            if not is_scientific_output_file(
                path,
                output_root=artifacts_dir,
                claim_identities=claim_identities,
            ):
                raise RunStoreIntegrityError(f"unsafe artifact ownership: {path.name}")
            relative = path.relative_to(artifacts_dir).as_posix()
            digest = self._hash_stable_file(path)
            after = os.lstat(path)
            if after.st_nlink != 1:
                raise RunStoreIntegrityError(f"unsafe artifact hard link: {path.name}")
            inventory.append(
                {
                    "path": relative,
                    "size_bytes": int(after.st_size),
                    "sha256": digest,
                    "media_type": mimetypes.guess_type(relative)[0]
                    or "application/octet-stream",
                }
            )
        return inventory

    def _hash_stable_file(self, path: Path) -> str:
        try:
            before = os.lstat(path)
            if (
                stat_is_filesystem_alias(before)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
            ):
                raise RunStoreIntegrityError(f"unsafe artifact: {path.name}")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            after = os.lstat(path)
        except OSError as exc:
            raise RunStoreIntegrityError(
                f"artifact cannot be verified: {path.name}"
            ) from exc
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or after.st_nlink != 1:
            raise RunStoreIntegrityError(f"artifact drifted while hashing: {path.name}")
        return digest.hexdigest()

    def _load(self, manifest_ref: str) -> tuple[Path, dict[str, Any]]:
        token = self._token(manifest_ref)
        reference_path = self._reference_path(token)
        reference = self._read_json(reference_path, "Run Store reference")
        if (
            set(reference)
            != {
                "schema_version",
                "token",
                "relative_root",
                "run_id",
                "header_sha256",
            }
            or reference.get("schema_version") != 1
            or reference.get("token") != token
        ):
            raise RunStoreIntegrityError("Run Store reference identity mismatch")
        raw_relative = reference.get("relative_root")
        if not isinstance(raw_relative, str):
            raise RunStoreIntegrityError("Run Store reference has no relative root")
        relative = PurePosixPath(raw_relative)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise RunStoreIntegrityError("Run Store reference root is unsafe")
        run_root = self.output_root.joinpath(*relative.parts)
        try:
            resolved = run_root.resolve(strict=True)
            resolved.relative_to(self.output_root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise RunStoreIntegrityError(
                "Run Store reference escapes its root"
            ) from exc
        if first_filesystem_alias_component(run_root) is not None:
            raise RunStoreIntegrityError("Run Store reference crosses an alias")
        manifest = self._read_json(run_root / _MANIFEST_FILENAME, "Run Manifest")
        if manifest.get("schema_version") != 1:
            raise RunStoreIntegrityError("unsupported Run Manifest schema")
        header = manifest.get("header")
        if not isinstance(header, dict):
            raise RunStoreIntegrityError("Run Manifest header is missing")
        header_bytes = canonical_json_bytes(header)
        if (
            reference.get("run_id") != header.get("run_id")
            or not isinstance(reference.get("header_sha256"), str)
            or hashlib.sha256(header_bytes).hexdigest()
            != reference.get("header_sha256")
        ):
            raise RunStoreIntegrityError("Run Store reference/header binding mismatch")
        return run_root, manifest

    def _read_json(self, path: Path, label: str) -> dict[str, Any]:
        try:
            details = os.lstat(path)
            if (
                stat_is_filesystem_alias(details)
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_size > _MAX_STORED_JSON_BYTES
            ):
                raise RunStoreIntegrityError(f"{label} is not an owned regular file")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except RunStoreIntegrityError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunStoreIntegrityError(f"{label} is unreadable") from exc
        if not isinstance(payload, dict):
            raise RunStoreIntegrityError(f"{label} must be a JSON object")
        return payload

    def _write_manifest(self, run_root: Path, manifest: Mapping[str, Any]) -> None:
        self._atomic_json(
            run_root / _MANIFEST_FILENAME,
            manifest,
            label="Run Manifest",
        )

    def _atomic_json(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        label: str,
    ) -> None:
        encoded = _canonical_store_json_bytes(payload).decode("utf-8")
        atomic_write_owned_output_text(
            path,
            output_root=self.output_root,
            text=encoded + "\n",
            label=label,
        )

    def _reference_path(self, token: str) -> Path:
        self._require_owned_directory(self._reference_root)
        return self._reference_root / f"{token}.json"

    def _ensure_owned_directory(self, path: Path) -> None:
        if first_filesystem_alias_component(path) is not None:
            raise RunStoreIntegrityError("Run Store directory crosses an alias")
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            try:
                path.mkdir(parents=False, exist_ok=False)
                details = os.lstat(path)
            except OSError as exc:
                raise RunStoreIntegrityError(
                    "Run Store directory cannot be created safely"
                ) from exc
        except OSError as exc:
            raise RunStoreIntegrityError("Run Store directory is unavailable") from exc
        if stat_is_filesystem_alias(details) or not stat.S_ISDIR(details.st_mode):
            raise RunStoreIntegrityError(
                "Run Store directory is not an owned directory"
            )
        self._require_owned_directory(path)

    def _require_owned_directory(self, path: Path) -> None:
        try:
            if first_filesystem_alias_component(path) is not None:
                raise RunStoreIntegrityError("Run Store directory crosses an alias")
            details = os.lstat(path)
            path.resolve(strict=True).relative_to(self.output_root.resolve(strict=True))
        except RunStoreIntegrityError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise RunStoreIntegrityError(
                "Run Store directory is outside its root"
            ) from exc
        if stat_is_filesystem_alias(details) or not stat.S_ISDIR(details.st_mode):
            raise RunStoreIntegrityError(
                "Run Store directory is not an owned directory"
            )

    @staticmethod
    def _token(manifest_ref: str) -> str:
        match = _REFERENCE_RE.fullmatch(str(manifest_ref))
        if match is None:
            raise RunStoreIntegrityError("invalid opaque Run Store reference")
        return match.group(1)

    def _remove_provisional_paths(self, token: str, run_root: Path) -> None:
        reference_path = self._reference_path(token)
        try:
            if reference_path.exists():
                reference_path.unlink()
            if run_root.exists():
                if (
                    not is_contained_output_path(run_root, output_root=self.output_root)
                    or first_filesystem_alias_component(run_root) is not None
                ):
                    raise RunStoreIntegrityError("provisional Run root is unsafe")
                shutil.rmtree(run_root)
        except OSError as exc:
            raise RunStoreIntegrityError("provisional Run root cleanup failed") from exc

    @staticmethod
    def _require_accepted_unfinished(manifest: Mapping[str, Any]) -> None:
        if manifest.get("acceptance", {}).get("state") != "accepted":
            raise RunStoreIntegrityError("Run Manifest is not accepted")
        if manifest.get("completion") is not None:
            raise RunStoreIntegrityError("Run Manifest completion already exists")


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        copied = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Run Manifest value must be canonical JSON") from exc
    if not isinstance(copied, dict):
        raise ValueError("Run Manifest value must be a JSON object")
    return copied


def _descriptor_identity(
    descriptor: int,
) -> tuple[int, int, int, int, int, int, int]:
    try:
        details = os.fstat(descriptor)
    except OSError as exc:
        raise RunStoreIntegrityError("verified artifact descriptor is unavailable") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise RunStoreIntegrityError("verified artifact descriptor is unsafe")
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        int(details.st_nlink),
    )


__all__ = [
    "FilesystemRunStore",
    "ProvisionalRunManifest",
    "RunCompletionEvidence",
    "RunHeaderProjection",
    "RunManifestHeader",
    "RunStoreTerminalProjection",
    "RunStoreIntegrityError",
    "VerifiedRunArtifact",
    "VerifiedRunArtifactFile",
    "VerifiedRunArtifactInventory",
    "VerifiedRunOutput",
]
