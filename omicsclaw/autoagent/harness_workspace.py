"""Isolated sandbox workspace for harness evolution.

The harness loop must never mutate the user's source worktree while a trial
is still provisional. This module snapshots the current repository into an
isolated git repo under the run output directory, then evaluates each trial in
its own temporary git worktree. Accepted patches are committed inside the
sandbox repo, exported as patch artifacts, and can be promoted back to the
source tree in a controlled final step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from uuid import uuid4

from omicsclaw.autoagent.edit_surface import EditSurface
from omicsclaw.autoagent.output_ownership import canonical_output_path
from omicsclaw.autoagent.patch_engine import Hunk, PatchPlan, render_hunks
from omicsclaw.common.output_claim import (
    atomic_write_owned_output_text,
    first_filesystem_alias_component,
    is_scientific_output_file,
    stat_is_filesystem_alias,
)
from omicsclaw.skill.execution.environment import (
    scrub_internal_control_credentials,
)

logger = logging.getLogger(__name__)

_ACCEPTED_MANIFEST_MAX_BYTES = 1024 * 1024
_ACCEPTED_PATCH_MAX_BYTES = 16 * 1024 * 1024
_GIT_CONFIG_AUTHORITY_MAX_BYTES = 4096
_GIT_CONTROL_STATE_MAX_BYTES = 16 * 1024 * 1024
_GIT_CONTROL_STATE_MAX_ENTRIES = 100_000
_ACCEPTED_COMMIT_SUBJECT_RE = re.compile(r"Harness iteration ([0-9]+): .+")
_PATCH_PLAN_KEYS = frozenset(
    {
        "target_files",
        "description",
        "expected_improvements",
        "rollback_conditions",
        "diffs",
        "reasoning",
        "converged",
    }
)
_PATCH_DIFF_KEYS = frozenset({"file", "hunks"})
_PATCH_HUNK_KEYS = frozenset({"old_code", "new_code"})
_GIT_MODE_TO_POSIX_MODE = {b"100644": 0o644, b"100755": 0o755}
_REGULAR_GIT_MODES = frozenset(_GIT_MODE_TO_POSIX_MODE)
_ACCEPTED_EVIDENCE_TRAILER = "OmicsClaw-Accepted-Evidence-SHA256"
_SOURCE_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})?")
_DETERMINISTIC_WORKTREE_GIT_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.ignoreStat=false",
    "-c",
    "core.fileMode=true",
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
)
_APPLIED_PROMOTION_MESSAGE = (
    "Promoted accepted sandbox state to the source worktree."
)
_APPLIED_JOURNAL_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "transaction_id",
        "source_project_root",
        "sandbox_repo",
        "baseline_commit",
        "accepted_commit",
        "files",
        "entries",
        "applied_files",
        "blocked_files",
        "timestamp",
        "message",
    }
)
_INTERRUPTED_JOURNAL_KEYS = _APPLIED_JOURNAL_KEYS - {"message"}
_PROMOTION_ENTRY_KEYS = frozenset(
    {
        "path",
        "stage_name",
        "backup_name",
        "expected_identity",
        "expected_digest",
        "expected_mode",
        "accepted_digest",
        "parent_chain_identities",
        "stage_identity",
        "phase",
        "installed_identity",
    }
)
_GIT_CONFIG_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "identity",
        "size",
        "mtime_ns",
        "ctime_ns",
        "mode",
        "digest",
    }
)
_RAW_CONTROL_ENTRY_KEYS = frozenset(
    {
        "kind",
        "identity",
        "size",
        "mtime_ns",
        "ctime_ns",
        "mode",
        "nlink",
        "digest",
    }
)
_GIT_CONTROL_CLEAN_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "generation",
        "source_project_root",
        "source_project_identity",
        "sandbox_repo",
        "sandbox_identity",
        "accepted_commit",
        "entry_count",
        "authority_digest",
        "entries",
    }
)
_GIT_CONTROL_OPEN_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "generation",
        "token",
        "iteration",
        "worktree",
        "clean_authority_digest",
    }
)


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"Duplicate JSON object key: {key}")
        payload[key] = value
    return payload


@dataclass
class AcceptedPatchRecord:
    """Durable metadata for a patch that was kept by the harness."""

    iteration: int
    commit_hash: str
    parent_commit: str
    artifact_path: str
    manifest_path: str
    modified_files: list[str] = field(default_factory=list)
    reasoning: str = ""
    diff_summary: str = ""
    description: str = ""
    expected_improvements: list[str] = field(default_factory=list)
    rollback_conditions: list[str] = field(default_factory=list)
    sandbox_repo: str = ""
    sandbox_worktree: str = ""
    source_project_commit: str = ""
    source_project_root: str = ""
    source_project_identity: tuple[int, int] = (0, 0)
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "commit_hash": self.commit_hash,
            "parent_commit": self.parent_commit,
            "artifact_path": self.artifact_path,
            "manifest_path": self.manifest_path,
            "modified_files": list(self.modified_files),
            "reasoning": self.reasoning,
            "diff_summary": self.diff_summary,
            "description": self.description,
            "expected_improvements": list(self.expected_improvements),
            "rollback_conditions": list(self.rollback_conditions),
            "sandbox_repo": self.sandbox_repo,
            "sandbox_worktree": self.sandbox_worktree,
            "source_project_commit": self.source_project_commit,
            "source_project_root": self.source_project_root,
            "source_project_identity": list(self.source_project_identity),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AcceptedPatchRecord:
        """Rebuild a successful record from a persisted harness result."""

        if not isinstance(payload, dict):
            raise ValueError("Accepted patch record must be an object.")
        iteration = payload.get("iteration")
        if not isinstance(iteration, int) or isinstance(iteration, bool):
            raise ValueError("Accepted patch record has an invalid iteration.")

        required_strings = {
            key: payload.get(key)
            for key in (
                "commit_hash",
                "parent_commit",
                "artifact_path",
                "manifest_path",
            )
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in required_strings.values()
        ):
            raise ValueError("Accepted patch record is missing durable identity.")

        source_project_root = payload.get("source_project_root")
        if (
            not isinstance(source_project_root, str)
            or not source_project_root
            or source_project_root.strip() != source_project_root
        ):
            raise ValueError(
                "Accepted patch record has an invalid source project root."
            )
        raw_source_identity = payload.get("source_project_identity")
        if (
            not isinstance(raw_source_identity, list)
            or len(raw_source_identity) != 2
            or not all(
                isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
                for item in raw_source_identity
            )
        ):
            raise ValueError(
                "Accepted patch record has an invalid source project identity."
            )

        def _string_list(key: str) -> list[str]:
            value = payload.get(key, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"Accepted patch record has invalid {key}.")
            return list(value)

        return cls(
            iteration=iteration,
            commit_hash=required_strings["commit_hash"],
            parent_commit=required_strings["parent_commit"],
            artifact_path=required_strings["artifact_path"],
            manifest_path=required_strings["manifest_path"],
            modified_files=_string_list("modified_files"),
            reasoning=str(payload.get("reasoning", "")),
            diff_summary=str(payload.get("diff_summary", "")),
            description=str(payload.get("description", "")),
            expected_improvements=_string_list("expected_improvements"),
            rollback_conditions=_string_list("rollback_conditions"),
            sandbox_repo=str(payload.get("sandbox_repo", "")),
            sandbox_worktree=str(payload.get("sandbox_worktree", "")),
            source_project_commit=str(payload.get("source_project_commit", "")),
            source_project_root=source_project_root,
            source_project_identity=(
                raw_source_identity[0],
                raw_source_identity[1],
            ),
            timestamp=str(payload.get("timestamp", "")),
        )


@dataclass
class PromotionResult:
    """Outcome of promoting the accepted sandbox state to the source tree."""

    status: str
    message: str = ""
    promoted_files: list[str] = field(default_factory=list)
    blocked_files: list[str] = field(default_factory=list)
    journal_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "promoted_files": list(self.promoted_files),
            "blocked_files": list(self.blocked_files),
            "journal_path": self.journal_path,
        }


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    digest: str


@dataclass(frozen=True, slots=True)
class _RawWorktreeEntry:
    kind: str
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    nlink: int
    digest: str


@dataclass(frozen=True, slots=True)
class _TrackedSourceEntry:
    rel_path: str
    git_mode: str
    content: bytes
    source_state: _RawWorktreeEntry


@dataclass(slots=True)
class _CandidateWorktreeAuthority:
    iteration: int
    parent_commit: str
    patch_plan_digest: str
    modified_files: tuple[str, ...]
    inventory: dict[str, _RawWorktreeEntry]


@dataclass(slots=True)
class _PromotionEntry:
    rel_path: str
    target: Path
    stage: Path
    backup: Path
    expected: _FileSnapshot
    accepted_digest: str
    parent_chain_identities: tuple[tuple[int, int], ...]
    phase: str = "prepared"
    stage_identity: tuple[int, int] | None = None
    installed_identity: tuple[int, int] | None = None

    def to_journal_dict(self) -> dict[str, Any]:
        return {
            "path": self.rel_path,
            "stage_name": self.stage.name,
            "backup_name": self.backup.name,
            "expected_identity": list(self.expected.identity),
            "expected_digest": self.expected.digest,
            "expected_mode": self.expected.mode,
            "accepted_digest": self.accepted_digest,
            "parent_chain_identities": [
                list(identity) for identity in self.parent_chain_identities
            ],
            "stage_identity": (
                list(self.stage_identity)
                if self.stage_identity is not None
                else None
            ),
            "phase": self.phase,
            "installed_identity": (
                list(self.installed_identity)
                if self.installed_identity is not None
                else None
            ),
        }


class _PromotionConflict(RuntimeError):
    """The source entry no longer matches its stable preflight snapshot."""


class _AcceptedEvidenceMustBeRetained(RuntimeError):
    """Candidate evidence is or may be reachable from accepted authority."""


class HarnessWorkspace:
    """Isolated snapshot repo that backs harness evolution trials."""

    def __init__(self, source_project_root: Path, output_root: Path) -> None:
        self.source_project_root = Path(source_project_root).expanduser().resolve()
        self.output_root = canonical_output_path(output_root)
        self.repo_root = self.output_root / "sandbox_repo"
        self.worktrees_root = self.output_root / "sandbox_worktrees"
        self.accepted_artifacts_root = self.output_root / "accepted_patches"
        self.promotion_journal_path = self.output_root / "promotion_state.json"
        self.git_control_compromise_path = (
            self.output_root / "git_control_compromised.json"
        )
        self.git_config_authority_path = (
            self.output_root / "git_config_authority.json"
        )
        self.git_control_state_path = self.output_root / "git_control_state.json"
        self.accepted_branch = "accepted"
        self.source_project_commit = ""
        self.baseline_commit = ""
        self.accepted_commit = ""
        self._source_project_authority: tuple[
            str,
            tuple[int, int],
        ] | None = None
        self._git_config_authority: _FileSnapshot | None = None
        self._git_control_generation = ""
        self._clean_git_control_digest = ""
        self._active_trial_token: str | None = None
        self._active_trial_worktree: Path | None = None
        self._git_control_state_verified = False
        self._rebuilding = False
        self._trial_worktree_authorities: dict[
            Path,
            tuple[str, dict[str, _RawWorktreeEntry]],
        ] = {}
        self._trial_editable_baselines: dict[Path, dict[str, bytes]] = {}
        self._trial_seed_blob_inventories: dict[
            Path,
            tuple[str, dict[str, tuple[str, str]]],
        ] = {}
        self._candidate_worktree_authorities: dict[
            Path,
            _CandidateWorktreeAuthority,
        ] = {}
        self._trial_git_control_authorities: dict[
            Path,
            dict[str, _RawWorktreeEntry],
        ] = {}
        self._unsafe_worktree_controls: set[Path] = set()
        self._git_control_compromised = os.path.lexists(
            self.git_control_compromise_path
        )
        self._created = False

    def create(self) -> None:
        """Materialize the isolated snapshot repo for this harness run."""
        self._created = False
        self._rebuilding = True
        self._git_config_authority = None
        self._git_control_generation = uuid4().hex
        self._clean_git_control_digest = ""
        self._active_trial_token = None
        self._active_trial_worktree = None
        self._git_control_state_verified = False
        self._trial_worktree_authorities = {}
        self._trial_editable_baselines = {}
        self._trial_seed_blob_inventories = {}
        self._candidate_worktree_authorities = {}
        self._trial_git_control_authorities = {}
        self._unsafe_worktree_controls = set()
        self._source_project_authority = None
        try:
            source_project_authority = self._capture_source_project_authority()
            (
                source_project_commit,
                source_index,
                tracked_entries,
            ) = self._capture_tracked_source_snapshot()
            self.output_root.mkdir(parents=True, exist_ok=True)
            self._reset_path(self.repo_root)
            self._reset_path(self.worktrees_root)
            self._reset_path(self.accepted_artifacts_root)
            self._copy_project_snapshot(tracked_entries)
            self._init_git_repo(tracked_entries)
            self._verify_tracked_source_snapshot(
                source_project_commit=source_project_commit,
                source_index=source_index,
                expected_entries=tracked_entries,
            )
            self.source_project_commit = source_project_commit
            self.baseline_commit = self._git_output(
                ["rev-parse", "HEAD^{commit}"],
                cwd=self.repo_root,
            )
            self.accepted_commit = self.baseline_commit
            self._git(
                ["branch", "-f", self.accepted_branch, self.accepted_commit],
                cwd=self.repo_root,
            )
            self._git_config_authority = self._snapshot_regular_file(
                self.repo_root / ".git" / "config"
            )
            self._persist_git_config_authority(self._git_config_authority)
            self._source_project_authority = source_project_authority
            self._replace_git_control_state_for_rebuild()
            self._persist_clean_git_control_state()
            # create() is the sole reset.  A prior latch is cleared only after
            # the replacement repository has a verified durable clean authority.
            self._clear_git_control_compromise_for_rebuild()
            self._git_control_compromised = False
            self._created = True
        finally:
            self._rebuilding = False
        logger.info(
            "Created harness sandbox repo at %s (baseline=%s)",
            self.repo_root,
            self.baseline_commit,
        )

    @contextmanager
    def trial_worktree(self, iteration: int, surface: EditSurface) -> Iterator[tuple[Path, EditSurface]]:
        """Yield a temporary git worktree plus a matching editable surface."""
        worktree = self.create_worktree(
            iteration,
            editable_files=list(surface.explicit_files),
        )
        try:
            yield worktree, surface.clone_for_project_root(worktree)
        finally:
            self.cleanup_worktree(worktree)

    def create_worktree(
        self,
        iteration: int,
        *,
        editable_files: list[str],
    ) -> Path:
        """Create an isolated worktree from the latest accepted commit."""
        if not self._created:
            raise RuntimeError("HarnessWorkspace.create() must be called first.")
        self._require_clean_git_control_authority(force=True)
        worktree = self.worktrees_root / f"iter_{iteration:04d}"
        canonical_editable_files = self._preflight_editable_files(editable_files)
        if os.path.lexists(worktree):
            raise ValueError(
                "Candidate worktree path already exists; rebuild the workspace."
            )
        parent_commit = self.accepted_commit
        self._begin_trial_git_control_state(
            iteration=iteration,
            worktree=worktree,
        )
        try:
            self._git(
                ["worktree", "add", "--detach", str(worktree), self.accepted_commit],
                cwd=self.repo_root,
            )
            self._authenticate_candidate_worktree(
                iteration=iteration,
                worktree=worktree,
                parent_commit=parent_commit,
            )
            initial_inventory = self._snapshot_raw_worktree(worktree)
            editable_baselines: dict[str, bytes] = {}
            for rel_path in canonical_editable_files:
                editable_baselines[rel_path] = self._read_stable_regular_bytes(
                    worktree.joinpath(*PurePosixPath(rel_path).parts)
                )
            self._trial_worktree_authorities[worktree] = (
                parent_commit,
                initial_inventory,
            )
            self._trial_editable_baselines[worktree] = editable_baselines
            self._require_worktree_status(
                worktree,
                parent_commit=parent_commit,
                expected_status=b" M",
                expected_files=[],
                stage="after worktree creation",
            )
            self._trial_git_control_authorities[worktree] = (
                self._snapshot_git_control(worktree)
            )
        except Exception:
            self._mark_git_control_compromised(
                worktree,
                reason="worktree_create_incomplete",
            )
            raise
        return worktree

    def _preflight_editable_files(self, editable_files: list[str]) -> list[str]:
        canonical: list[str] = []
        seen: set[str] = set()
        for raw_path in editable_files:
            rel_path = self._canonical_repo_path(
                raw_path,
                label="editable baseline path",
            )
            if rel_path in seen:
                raise ValueError("Editable baseline paths contain duplicates.")
            seen.add(rel_path)
            canonical.append(rel_path)
        object_format = self._git_output(
            ["rev-parse", "--show-object-format"],
            cwd=self.repo_root,
        )
        inventory = self._commit_blob_inventory(
            self.accepted_commit,
            object_format=object_format,
        )
        for rel_path in canonical:
            entry = inventory.get(rel_path)
            if entry is None:
                raise ValueError(
                    "Editable file is not in the governed tracked baseline; "
                    f"track it in source Git first: {rel_path}"
                )
            if entry[0] not in {"100644", "100755"}:
                raise ValueError(
                    f"Editable file is not a regular Git blob: {rel_path}"
                )
        return canonical

    def require_clean_baseline_worktree(self, worktree: Path) -> None:
        """Authenticate an unmodified iteration-zero worktree after execution."""

        self._require_git_control_uncompromised()
        if self.accepted_commit != self.baseline_commit:
            raise ValueError("Baseline worktree authority is no longer current.")
        canonical_worktree = self._canonical_trial_worktree_path(
            iteration=0,
            worktree=worktree,
        )
        # Verify raw control bytes before any Git command can consult a hook,
        # attribute, exclude, index, or repository-local configuration changed
        # by baseline execution.
        self._verify_git_control_witness(canonical_worktree, consume=True)
        canonical_worktree = self._authenticate_candidate_worktree(
            iteration=0,
            worktree=canonical_worktree,
            parent_commit=self.baseline_commit,
        )
        self._require_worktree_status(
            canonical_worktree,
            parent_commit=self.baseline_commit,
            expected_status=b" M",
            expected_files=[],
            stage="after baseline execution",
        )

    def cleanup_worktree(self, worktree: Path) -> None:
        """Remove a temporary worktree after the iteration completes."""
        canonical_worktree = Path(os.path.abspath(Path(worktree).expanduser()))
        try:
            relative = canonical_worktree.relative_to(self.worktrees_root)
        except ValueError:
            relative = Path(".")
        if (
            canonical_worktree != worktree
            or len(relative.parts) != 1
            or re.fullmatch(r"iter_[0-9]{4,}", relative.name) is None
        ):
            self._mark_git_control_compromised(
                canonical_worktree,
                reason="worktree_cleanup_path_invalid",
            )
            raise RuntimeError(
                "Sandbox worktree cleanup failed for a non-canonical path."
            )
        worktree = canonical_worktree
        if self._git_control_compromised:
            raise RuntimeError(
                "Sandbox worktree cleanup is blocked because Git control "
                "authority is compromised."
            )
        if worktree in self._unsafe_worktree_controls:
            raise RuntimeError(
                "Sandbox worktree cleanup is blocked after Git control drift."
            )
        if worktree in self._trial_git_control_authorities:
            try:
                # Rejected/crashed trials never reach the commit boundary, so
                # cleanup is their first opportunity to prove that invoking
                # Git is still safe.  Drift retains the worktree for manual
                    # inspection and deliberately avoids candidate-controlled Git.
                self._verify_git_control_witness(worktree, consume=True)
            except ValueError as exc:
                raise RuntimeError(
                    "Sandbox worktree cleanup failed after Git control drift."
                ) from exc

        registration = self.repo_root / ".git" / "worktrees" / worktree.name
        if not os.path.lexists(worktree):
            self._mark_git_control_compromised(
                worktree,
                reason="worktree_path_missing_before_remove",
            )
            raise RuntimeError(
                "Sandbox worktree cleanup failed because its authority "
                "disappeared before the owned remove operation."
            )
        try:
            self._git(["worktree", "remove", "--force", str(worktree)], cwd=self.repo_root)
        except ValueError as exc:
            self._mark_git_control_compromised(
                worktree,
                reason="worktree_remove_authority_drift",
            )
            raise RuntimeError(
                "Sandbox worktree cleanup failed after Git authority drift."
            ) from exc
        except RuntimeError as exc:
            self._mark_git_control_compromised(
                worktree,
                reason="worktree_remove_failed",
            )
            raise RuntimeError(
                "Sandbox worktree cleanup failed; candidate evidence was retained."
            ) from exc

        if os.path.lexists(worktree) or os.path.lexists(registration):
            self._mark_git_control_compromised(
                worktree,
                reason="worktree_remove_incomplete",
            )
            raise RuntimeError(
                "Sandbox worktree cleanup failed verification; candidate "
                "evidence was retained."
            )
        try:
            self._persist_clean_git_control_state()
        except (OSError, RuntimeError, ValueError) as exc:
            self._mark_git_control_compromised(
                worktree,
                reason="worktree_cleanup_checkpoint_failed",
            )
            raise RuntimeError(
                "Sandbox worktree cleanup could not publish clean Git authority."
            ) from exc
        self._clear_worktree_authority(worktree)
        self._active_trial_token = None
        self._active_trial_worktree = None

    def _clear_worktree_authority(self, worktree: Path) -> None:
        """Forget in-memory trial authority only after durable cleanup proof."""

        self._trial_worktree_authorities.pop(worktree, None)
        self._trial_editable_baselines.pop(worktree, None)
        self._trial_seed_blob_inventories.pop(worktree, None)
        self._candidate_worktree_authorities.pop(worktree, None)
        self._trial_git_control_authorities.pop(worktree, None)

    def freeze_candidate_patch(
        self,
        *,
        iteration: int,
        worktree: Path,
        patch: PatchPlan,
        modified_files: list[str],
    ) -> None:
        """Bind the exact post-apply raw state before candidate execution.

        This is the first half of the candidate witness.  It runs immediately
        after :func:`apply_patch`, before the trial imports or executes any
        candidate code.  The accepted-commit boundary later requires the same
        witness byte-for-byte, so runtime-created files, mode changes, and
        ignored state cannot become part of an accepted observation.
        """

        if not self._created:
            raise RuntimeError("HarnessWorkspace.create() must be called first.")
        self._require_git_control_uncompromised()
        if (
            not isinstance(iteration, int)
            or isinstance(iteration, bool)
            or iteration <= 0
        ):
            raise ValueError("Candidate patch iteration must be a positive integer.")

        frozen_plan = self._freeze_patch_plan(patch)
        plan_files, _hunk_count = self._validate_patch_plan_payload(frozen_plan)
        canonical_files = self._canonical_modified_files(modified_files)
        if canonical_files != plan_files:
            raise ValueError(
                "Candidate modified_files must exactly match PatchPlan targets."
            )

        canonical_worktree = self._canonical_trial_worktree_path(
            iteration=iteration,
            worktree=worktree,
        )
        # No Git command may precede this raw control comparison.
        self._verify_git_control_witness(canonical_worktree, consume=False)

        seed = self._trial_worktree_authorities.get(canonical_worktree)
        if seed is None or seed[0] != self.accepted_commit:
            raise ValueError("Candidate raw seed is unavailable or stale.")
        parent_commit, initial = seed
        editable_baselines = self._trial_editable_baselines.get(canonical_worktree)
        if editable_baselines is None or not set(canonical_files).issubset(
            editable_baselines
        ):
            raise ValueError("Candidate targets are outside the frozen edit surface.")

        current = self._snapshot_raw_worktree(canonical_worktree)
        if set(current) != set(initial):
            raise ValueError("Candidate raw paths changed while applying its patch.")

        mutable = set(canonical_files)
        for rel_path, initial_entry in initial.items():
            current_entry = current[rel_path]
            if rel_path not in mutable:
                if current_entry != initial_entry:
                    raise ValueError(
                        "Candidate changed raw state outside PatchPlan targets."
                    )
                continue
            if (
                initial_entry.kind != "regular"
                or current_entry.kind != "regular"
                or initial_entry.identity != current_entry.identity
                or initial_entry.mode != current_entry.mode
                or initial_entry.nlink != 1
                or current_entry.nlink != 1
            ):
                raise ValueError(
                    "Candidate target identity, type, link count, or mode changed."
                )

        for raw_diff in frozen_plan["diffs"]:
            rel_path = raw_diff["file"]
            # read_text(), used by validate/apply, performs universal newline
            # translation.  Reproduce that exact parent-text contract both
            # here and when replaying the durable Git blob below.
            parent_text = self._patch_parent_text(
                editable_baselines[rel_path],
                rel_path=rel_path,
            )
            hunks = [
                Hunk(
                    old_code=raw_hunk["old_code"],
                    new_code=raw_hunk["new_code"],
                )
                for raw_hunk in raw_diff["hunks"]
            ]
            expected_bytes = render_hunks(
                parent_text,
                hunks,
                rel_path=rel_path,
            ).encode("utf-8")
            candidate_path = canonical_worktree.joinpath(
                *PurePosixPath(rel_path).parts
            )
            if self._read_stable_regular_bytes(candidate_path) != expected_bytes:
                raise ValueError(
                    "Candidate raw target bytes do not match PatchPlan output for "
                    f"{rel_path}."
                )

        # Close the read window: the inventory stored as authority must be the
        # same state whose target bytes were just checked.
        final_inventory = self._snapshot_raw_worktree(canonical_worktree)
        if final_inventory != current:
            raise ValueError("Candidate raw state changed while being frozen.")
        self._candidate_worktree_authorities[canonical_worktree] = (
            _CandidateWorktreeAuthority(
                iteration=iteration,
                parent_commit=parent_commit,
                patch_plan_digest=self._patch_plan_digest(frozen_plan),
                modified_files=tuple(canonical_files),
                inventory=final_inventory,
            )
        )

    def commit_accepted_patch(
        self,
        iteration: int,
        worktree: Path,
        patch: PatchPlan,
        modified_files: list[str],
    ) -> AcceptedPatchRecord:
        """Authenticate and atomically publish one accepted sandbox commit.

        The accepted ref is the final authority transition.  Everything that
        describes the candidate -- parent, worktree, index delta, PatchPlan,
        commit tree, manifest, and patch artifact -- is authenticated before
        that compare-and-swap.  A post-CAS verification failure rolls the ref
        back with the inverse CAS before candidate evidence is removed.
        """

        if not self._created:
            raise RuntimeError("HarnessWorkspace.create() must be called first.")
        self._require_git_control_uncompromised()
        if (
            not isinstance(iteration, int)
            or isinstance(iteration, bool)
            or iteration <= 0
        ):
            raise ValueError("Accepted patch iteration must be a positive integer.")

        frozen_plan = self._freeze_patch_plan(patch)
        plan_files, hunk_count = self._validate_patch_plan_payload(frozen_plan)
        canonical_files = self._canonical_modified_files(modified_files)
        if canonical_files != plan_files:
            raise ValueError(
                "Accepted modified_files must exactly match PatchPlan targets."
            )

        canonical_worktree = self._canonical_trial_worktree_path(
            iteration=iteration,
            worktree=worktree,
        )
        # The second witness check must happen before *any* Git operation.
        # Once consumed, no candidate code runs again and subsequent Git calls
        # operate only on the state proven here.
        self._verify_git_control_witness(canonical_worktree, consume=True)
        candidate_authority = self._candidate_worktree_authorities.get(
            canonical_worktree
        )
        if (
            candidate_authority is None
            or candidate_authority.iteration != iteration
            or candidate_authority.parent_commit != self.accepted_commit
            or candidate_authority.modified_files != tuple(canonical_files)
            or candidate_authority.patch_plan_digest
            != self._patch_plan_digest(frozen_plan)
        ):
            raise ValueError(
                "Accepted candidate has no matching pre-execution witness."
            )

        self._authenticate_sandbox_git_authority()
        parent_commit = self.accepted_commit
        accepted_head = self._git_output(
            ["rev-parse", f"refs/heads/{self.accepted_branch}^{{commit}}"],
            cwd=self.repo_root,
        )
        if accepted_head != parent_commit:
            raise ValueError(
                "Accepted branch head does not match the workspace parent cache."
            )
        parent_record = self._authenticate_current_parent(parent_commit)
        if parent_record is not None and iteration <= parent_record.iteration:
            raise ValueError(
                "Accepted patch iteration must advance past its accepted parent."
            )
        canonical_worktree = self._authenticate_candidate_worktree(
            iteration=iteration,
            worktree=canonical_worktree,
            parent_commit=parent_commit,
        )
        self._require_worktree_status(
            canonical_worktree,
            parent_commit=parent_commit,
            expected_status=b" M",
            expected_files=canonical_files,
            stage="before staging",
        )
        source_project_root, source_project_identity = (
            self._require_source_project_authority()
        )

        self._git(["add", "--", *canonical_files], cwd=canonical_worktree)
        self._require_worktree_status(
            canonical_worktree,
            parent_commit=parent_commit,
            expected_status=b"M ",
            expected_files=canonical_files,
            stage="after staging",
        )
        tree_hash = self._git_output(["write-tree"], cwd=canonical_worktree)
        staged_files = self._changed_regular_files(parent_commit, tree_hash)
        if staged_files != sorted(canonical_files):
            raise ValueError(
                "Staged Git delta does not exactly match accepted modified_files."
            )
        self._verify_patch_plan_bytes(
            parent_commit=parent_commit,
            candidate_treeish=tree_hash,
            patch_plan=frozen_plan,
        )

        record = AcceptedPatchRecord(
            iteration=iteration,
            commit_hash="",
            parent_commit=parent_commit,
            artifact_path="",
            manifest_path="",
            modified_files=list(canonical_files),
            reasoning=frozen_plan["reasoning"],
            diff_summary=(
                f"{len(frozen_plan['diffs'])} file(s), {hunk_count} hunk(s)"
            ),
            description=frozen_plan["description"],
            expected_improvements=list(frozen_plan["expected_improvements"]),
            rollback_conditions=list(frozen_plan["rollback_conditions"]),
            sandbox_repo=str(self.repo_root),
            sandbox_worktree=str(canonical_worktree),
            source_project_commit=self.source_project_commit,
            source_project_root=source_project_root,
            source_project_identity=source_project_identity,
        )
        self._validate_record_provenance(record)
        message = self._build_commit_message_with_evidence_digest(
            iteration,
            frozen_plan,
            record,
            hunk_count=hunk_count,
        )
        commit_hash = self._git_output_with_input(
            ["commit-tree", tree_hash, "-p", parent_commit],
            cwd=self.repo_root,
            input_text=f"{message}\n",
        )
        ancestry = self._git_output(
            ["rev-list", "--parents", "-n", "1", commit_hash],
            cwd=self.repo_root,
        ).split()
        committed_tree = self._git_output(
            ["rev-parse", f"{commit_hash}^{{tree}}"],
            cwd=self.repo_root,
        )
        if ancestry != [commit_hash, parent_commit] or committed_tree != tree_hash:
            raise ValueError("Accepted candidate commit identity is invalid.")
        self._verify_patch_plan_bytes(
            parent_commit=parent_commit,
            candidate_treeish=commit_hash,
            patch_plan=frozen_plan,
        )

        short_hash = commit_hash[:12]
        artifact_path = self.accepted_artifacts_root / f"iter_{iteration:04d}_{short_hash}.patch"
        manifest_path = self.accepted_artifacts_root / f"iter_{iteration:04d}_{short_hash}.json"
        artifact_text = self._git_output(["format-patch", "--stdout", "-1", commit_hash], cwd=self.repo_root)
        record.commit_hash = commit_hash
        record.artifact_path = str(artifact_path)
        record.manifest_path = str(manifest_path)
        ref_advanced = False
        try:
            atomic_write_owned_output_text(
                artifact_path,
                output_root=self.output_root,
                text=artifact_text,
                label="accepted patch artifact",
            )
            atomic_write_owned_output_text(
                manifest_path,
                output_root=self.output_root,
                text=json.dumps(
                    record.to_dict() | {"patch_plan": frozen_plan},
                    indent=2,
                ),
                label="accepted patch manifest",
            )
            self._authenticate_accepted_chain(
                accepted_commit=commit_hash,
                supplied_record=record,
                supplied_patch_plan=frozen_plan,
            )
            self._git(
                [
                    "update-ref",
                    f"refs/heads/{self.accepted_branch}",
                    commit_hash,
                    parent_commit,
                ],
                cwd=self.repo_root,
            )
            ref_advanced = True
            if self._git_output(
                ["rev-parse", f"refs/heads/{self.accepted_branch}^{{commit}}"],
                cwd=self.repo_root,
            ) != commit_hash:
                raise RuntimeError("Accepted ref changed after candidate CAS.")
            self._authenticate_accepted_chain(
                accepted_commit=commit_hash,
                supplied_record=record,
                supplied_patch_plan=frozen_plan,
            )
        except Exception as exc:
            if ref_advanced:
                try:
                    self._git(
                        [
                            "update-ref",
                            f"refs/heads/{self.accepted_branch}",
                            parent_commit,
                            commit_hash,
                        ],
                        cwd=self.repo_root,
                    )
                    if self._git_output(
                        [
                            "rev-parse",
                            f"refs/heads/{self.accepted_branch}^{{commit}}",
                        ],
                        cwd=self.repo_root,
                    ) != parent_commit:
                        raise RuntimeError(
                            "Accepted ref rollback did not restore its parent."
                        )
                except Exception as rollback_exc:
                    raise RuntimeError(
                        "Accepted candidate verification failed after CAS and "
                        "the ref could not be rolled back; candidate evidence "
                        "was retained for manual recovery."
                    ) from rollback_exc
            try:
                with self._accepted_ref_cleanup_lock():
                    current_head = self._git_output(
                        [
                            "rev-parse",
                            f"refs/heads/{self.accepted_branch}^{{commit}}",
                        ],
                        cwd=self.repo_root,
                    )
                    reachable = current_head == commit_hash
                    if not reachable:
                        ancestry_check = self._git(
                            [
                                "merge-base",
                                "--is-ancestor",
                                commit_hash,
                                current_head,
                            ],
                            cwd=self.repo_root,
                            check=False,
                        )
                        if ancestry_check.returncode not in {0, 1}:
                            raise _AcceptedEvidenceMustBeRetained(
                                "Accepted candidate reachability is indeterminate."
                            )
                        reachable = ancestry_check.returncode == 0
                    if reachable:
                        raise _AcceptedEvidenceMustBeRetained(
                            "Accepted candidate evidence remains referenced by "
                            "the accepted chain."
                        )
                    self._remove_owned_metadata_file(manifest_path)
                    self._remove_owned_metadata_file(artifact_path)
            except _AcceptedEvidenceMustBeRetained as retention_exc:
                raise RuntimeError(
                    f"{retention_exc} Evidence was retained."
                ) from exc
            except Exception as cleanup_exc:
                raise RuntimeError(
                    "Accepted candidate evidence could not be safely locked and "
                    "cleaned; evidence was retained."
                ) from cleanup_exc
            if ref_advanced and self._git_output(
                ["rev-parse", f"refs/heads/{self.accepted_branch}^{{commit}}"],
                cwd=self.repo_root,
            ) != parent_commit:
                raise RuntimeError(
                    "Accepted candidate evidence cannot be cleaned while referenced."
                ) from exc
            raise
        self.accepted_commit = commit_hash
        return record

    def _freeze_patch_plan(self, patch: PatchPlan) -> dict[str, Any]:
        """Take one strict JSON deep snapshot of caller-owned PatchPlan state."""

        try:
            raw_payload = patch.to_dict()
            encoded = json.dumps(
                raw_payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            frozen = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Accepted PatchPlan is not strict JSON data.") from exc
        if frozen != raw_payload:
            raise ValueError("Accepted PatchPlan is not canonically JSON-shaped.")
        return frozen

    @staticmethod
    def _patch_plan_digest(patch_plan: dict[str, Any]) -> str:
        encoded = json.dumps(
            patch_plan,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _validate_patch_plan_payload(
        cls,
        payload: dict[str, Any],
    ) -> tuple[list[str], int]:
        """Validate the exact durable PatchPlan schema and return its file set."""

        if not isinstance(payload, dict) or set(payload) != _PATCH_PLAN_KEYS:
            raise ValueError("Accepted PatchPlan schema is invalid.")
        if payload.get("converged") is not False:
            raise ValueError("An accepted PatchPlan cannot be converged.")
        for key in ("description", "reasoning"):
            if not isinstance(payload.get(key), str):
                raise ValueError(f"Accepted PatchPlan {key} must be a string.")
        for key in ("expected_improvements", "rollback_conditions"):
            value = payload.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(
                    f"Accepted PatchPlan {key} must be a list of strings."
                )

        raw_targets = payload.get("target_files")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ValueError("Accepted PatchPlan target_files must be non-empty.")
        target_files = [
            cls._canonical_repo_path(value, label="PatchPlan target")
            for value in raw_targets
        ]
        if len(target_files) != len(set(target_files)):
            raise ValueError("Accepted PatchPlan has duplicate target_files.")

        raw_diffs = payload.get("diffs")
        if not isinstance(raw_diffs, list) or not raw_diffs:
            raise ValueError("Accepted PatchPlan diffs must be non-empty.")
        diff_files: list[str] = []
        hunk_count = 0
        for diff_index, raw_diff in enumerate(raw_diffs):
            if not isinstance(raw_diff, dict) or set(raw_diff) != _PATCH_DIFF_KEYS:
                raise ValueError(
                    f"Accepted PatchPlan diff #{diff_index} schema is invalid."
                )
            diff_files.append(
                cls._canonical_repo_path(
                    raw_diff.get("file"),
                    label=f"PatchPlan diff #{diff_index}",
                )
            )
            raw_hunks = raw_diff.get("hunks")
            if not isinstance(raw_hunks, list) or not raw_hunks:
                raise ValueError(
                    f"Accepted PatchPlan diff #{diff_index} has no hunks."
                )
            for hunk_index, raw_hunk in enumerate(raw_hunks):
                if (
                    not isinstance(raw_hunk, dict)
                    or set(raw_hunk) != _PATCH_HUNK_KEYS
                    or not isinstance(raw_hunk.get("old_code"), str)
                    or not raw_hunk["old_code"]
                    or not isinstance(raw_hunk.get("new_code"), str)
                    or raw_hunk["old_code"] == raw_hunk["new_code"]
                ):
                    raise ValueError(
                        "Accepted PatchPlan hunk is invalid at "
                        f"diff #{diff_index}, hunk #{hunk_index}."
                    )
                hunk_count += 1

        if len(diff_files) != len(set(diff_files)):
            raise ValueError("Accepted PatchPlan has duplicate diff files.")
        if target_files != diff_files:
            raise ValueError(
                "Accepted PatchPlan target_files must exactly match diff files."
            )
        return diff_files, hunk_count

    @classmethod
    def _canonical_modified_files(cls, modified_files: list[str]) -> list[str]:
        if not isinstance(modified_files, list) or not modified_files:
            raise ValueError("Accepted patch recorded no modified files.")
        canonical = [
            cls._canonical_repo_path(value, label="accepted modified file")
            for value in modified_files
        ]
        if len(canonical) != len(set(canonical)):
            raise ValueError("Accepted patch has duplicate modified files.")
        return canonical

    @staticmethod
    def _canonical_repo_path(value: Any, *, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value.strip() != value
            or "\\" in value
        ):
            raise ValueError(f"{label} is not a canonical repository path.")
        pure_path = PurePosixPath(value)
        if (
            pure_path.is_absolute()
            or pure_path.as_posix() != value
            or any(part in {"", ".", ".."} for part in pure_path.parts)
        ):
            raise ValueError(f"{label} is not a canonical repository path.")
        return value

    def _authenticate_current_parent(
        self,
        parent_commit: str,
    ) -> AcceptedPatchRecord | None:
        if not self.baseline_commit:
            raise ValueError("Harness workspace has no baseline commit authority.")
        if parent_commit == self.baseline_commit:
            ancestry = self._git_output(
                ["rev-list", "--parents", "-n", "1", parent_commit],
                cwd=self.repo_root,
            ).split()
            if ancestry != [parent_commit]:
                raise ValueError("Harness baseline is not the unique root commit.")
            return None
        baseline, _files, record = self._authenticate_accepted_chain(
            accepted_commit=parent_commit,
        )
        if baseline != self.baseline_commit:
            raise ValueError("Accepted parent chain does not reach the workspace baseline.")
        return record

    def _authenticate_sandbox_git_authority(self) -> Path:
        """Reject aliases or non-plain entries in the sandbox Git authority."""

        common_dir = self.repo_root / ".git"
        try:
            common_dir.relative_to(self.output_root)
            for directory in (self.output_root, self.repo_root, common_dir):
                if first_filesystem_alias_component(directory) is not None:
                    raise ValueError("Sandbox Git authority contains an alias.")
                directory_stat = os.lstat(directory)
                if (
                    stat_is_filesystem_alias(directory_stat)
                    or not stat.S_ISDIR(directory_stat.st_mode)
                ):
                    raise ValueError(
                        "Sandbox Git authority is not a plain directory."
                    )

            pending = [common_dir]
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entry_stat = entry.stat(follow_symlinks=False)
                        if stat_is_filesystem_alias(entry_stat):
                            raise ValueError(
                                "Sandbox Git authority contains an alias."
                            )
                        if stat.S_ISDIR(entry_stat.st_mode):
                            pending.append(Path(entry.path))
                        elif (
                            not stat.S_ISREG(entry_stat.st_mode)
                            or entry_stat.st_nlink != 1
                        ):
                            raise ValueError(
                                "Sandbox Git authority contains a non-plain entry."
                            )
        except ValueError:
            raise
        except (OSError, RuntimeError) as exc:
            raise ValueError("Sandbox Git authority cannot be authenticated.") from exc
        if self._created:
            if self._git_config_authority is None:
                self._git_config_authority = (
                    self._load_persisted_git_config_authority()
                )
            try:
                current_config = self._snapshot_regular_file(common_dir / "config")
            except _PromotionConflict as exc:
                raise ValueError("Sandbox Git config cannot be authenticated.") from exc
            if current_config != self._git_config_authority:
                raise ValueError("Sandbox Git config authority changed.")
        return common_dir

    def _persist_git_config_authority(self, snapshot: _FileSnapshot) -> None:
        payload = {
            "schema_version": 1,
            "identity": list(snapshot.identity),
            "size": snapshot.size,
            "mtime_ns": snapshot.mtime_ns,
            "ctime_ns": snapshot.ctime_ns,
            "mode": snapshot.mode,
            "digest": snapshot.digest,
        }
        atomic_write_owned_output_text(
            self.git_config_authority_path,
            output_root=self.output_root,
            text=json.dumps(payload, sort_keys=True) + "\n",
            label="Git config authority",
        )

    def _load_persisted_git_config_authority(self) -> _FileSnapshot:
        try:
            raw = self._read_plain_git_control_file(
                self.git_config_authority_path,
                label="Persisted Git config authority",
                max_bytes=_GIT_CONFIG_AUTHORITY_MAX_BYTES,
            )
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_json_object_without_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "Persisted Git config authority cannot be authenticated."
            ) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != _GIT_CONFIG_AUTHORITY_KEYS
            or payload.get("schema_version") != 1
        ):
            raise ValueError("Persisted Git config authority schema is invalid.")
        identity = payload.get("identity")
        integer_keys = ("size", "mtime_ns", "ctime_ns", "mode")
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for value in identity
            )
            or not all(
                isinstance(payload.get(key), int)
                and not isinstance(payload[key], bool)
                and payload[key] >= 0
                for key in integer_keys
            )
            or payload["mode"] > 0o7777
            or not isinstance(payload.get("digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["digest"]) is None
        ):
            raise ValueError("Persisted Git config authority values are invalid.")
        return _FileSnapshot(
            identity=(identity[0], identity[1]),
            size=payload["size"],
            mtime_ns=payload["mtime_ns"],
            ctime_ns=payload["ctime_ns"],
            mode=payload["mode"],
            digest=payload["digest"],
        )

    @staticmethod
    def _raw_control_entry_payload(entry: _RawWorktreeEntry) -> dict[str, Any]:
        return {
            "kind": entry.kind,
            "identity": list(entry.identity),
            "size": entry.size,
            "mtime_ns": entry.mtime_ns,
            "ctime_ns": entry.ctime_ns,
            "mode": entry.mode,
            "nlink": entry.nlink,
            "digest": entry.digest,
        }

    @classmethod
    def _raw_control_entry_from_payload(
        cls,
        payload: object,
    ) -> _RawWorktreeEntry:
        if not isinstance(payload, dict) or set(payload) != _RAW_CONTROL_ENTRY_KEYS:
            raise ValueError("Persisted Git control entry schema is invalid.")
        identity = payload.get("identity")
        integers = ("size", "mtime_ns", "ctime_ns", "mode", "nlink")
        digest = payload.get("digest")
        if (
            payload.get("kind") not in {"directory", "regular", "symlink"}
            or not isinstance(identity, list)
            or len(identity) != 2
            or not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for value in identity
            )
            or not all(
                isinstance(payload.get(key), int)
                and not isinstance(payload[key], bool)
                and payload[key] >= 0
                for key in integers
            )
            or payload["mode"] > 0o7777
            or not isinstance(digest, str)
            or (
                payload["kind"] == "directory"
                and digest != ""
            )
            or (
                payload["kind"] != "directory"
                and re.fullmatch(r"[0-9a-f]{64}", digest) is None
            )
        ):
            raise ValueError("Persisted Git control entry values are invalid.")
        return _RawWorktreeEntry(
            kind=payload["kind"],
            identity=(identity[0], identity[1]),
            size=payload["size"],
            mtime_ns=payload["mtime_ns"],
            ctime_ns=payload["ctime_ns"],
            mode=payload["mode"],
            nlink=payload["nlink"],
            digest=digest,
        )

    @classmethod
    def _git_control_entries_digest(
        cls,
        entries: dict[str, _RawWorktreeEntry],
    ) -> str:
        payload = {
            key: cls._raw_control_entry_payload(value)
            for key, value in sorted(entries.items())
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls._sha256(encoded)

    def _snapshot_persistable_git_control(
        self,
    ) -> dict[str, _RawWorktreeEntry]:
        common_dir = self.repo_root / ".git"
        entries = {
            f"common/{key}": value
            for key, value in self._snapshot_plain_tree(
                common_dir,
                excluded_root_names=set(),
                label="persisted sandbox Git control",
            ).items()
        }
        if any(
            key.startswith("common/worktrees/") or key.endswith(".lock")
            for key in entries
        ):
            raise ValueError(
                "Clean Git control contains a linked worktree or lock residue."
            )
        config_authority = self._snapshot_regular_file(
            self.git_config_authority_path
        )
        entries["persisted-config-authority"] = self._raw_regular_entry(
            config_authority
        )
        if len(entries) > _GIT_CONTROL_STATE_MAX_ENTRIES:
            raise ValueError("Persisted Git control inventory is too large.")
        return entries

    def _clean_git_control_payload(
        self,
        entries: dict[str, _RawWorktreeEntry],
    ) -> dict[str, Any]:
        source_root, source_identity = self._require_source_project_authority()
        try:
            sandbox_stat = os.lstat(self.repo_root)
        except OSError as exc:
            raise ValueError("Sandbox repository identity is unavailable.") from exc
        if (
            stat_is_filesystem_alias(sandbox_stat)
            or not stat.S_ISDIR(sandbox_stat.st_mode)
        ):
            raise ValueError("Sandbox repository identity is invalid.")
        entry_payload = {
            key: self._raw_control_entry_payload(value)
            for key, value in sorted(entries.items())
        }
        return {
            "schema_version": 1,
            "status": "clean",
            "generation": self._git_control_generation,
            "source_project_root": source_root,
            "source_project_identity": list(source_identity),
            "sandbox_repo": str(self.repo_root),
            "sandbox_identity": [sandbox_stat.st_dev, sandbox_stat.st_ino],
            "accepted_commit": self.accepted_commit,
            "entry_count": len(entries),
            "authority_digest": self._git_control_entries_digest(entries),
            "entries": entry_payload,
        }

    def _write_git_control_state(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        encoded_bytes = encoded.encode("utf-8")
        if len(encoded_bytes) > _GIT_CONTROL_STATE_MAX_BYTES:
            raise ValueError("Persisted Git control state is too large.")
        try:
            atomic_write_owned_output_text(
                self.git_control_state_path,
                output_root=self.output_root,
                text=encoded,
                label="Git control state",
            )
        except (OSError, RuntimeError, ValueError) as write_error:
            # ``atomic_write_owned_output_text`` can report a directory-fsync
            # failure after ``os.replace`` has already made the requested
            # state visible.  Under this workspace's documented non-power-loss
            # model, a bounded no-follow stable read of those exact bytes is a
            # successful publication.  Any absent, aliased, unstable, or
            # different state preserves the original fail-closed result.
            try:
                visible = self._read_plain_git_control_file(
                    self.git_control_state_path,
                    label="Persisted Git control state after write error",
                    max_bytes=_GIT_CONTROL_STATE_MAX_BYTES,
                )
            except ValueError:
                raise write_error
            if visible != encoded_bytes:
                raise write_error

    def _load_git_control_state(self) -> dict[str, Any]:
        try:
            raw = self._read_plain_git_control_file(
                self.git_control_state_path,
                label="Persisted Git control state",
                max_bytes=_GIT_CONTROL_STATE_MAX_BYTES,
            )
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_json_object_without_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "Persisted Git control state cannot be authenticated."
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("Persisted Git control state schema is invalid.")
        status = payload.get("status")
        if status == "clean":
            self._validate_clean_git_control_payload(payload)
        elif status == "trial_open":
            self._validate_open_git_control_payload(payload)
        else:
            raise ValueError("Persisted Git control state status is invalid.")
        return payload

    @classmethod
    def _validate_clean_git_control_payload(cls, payload: dict[str, Any]) -> None:
        if set(payload) != _GIT_CONTROL_CLEAN_KEYS:
            raise ValueError("Persisted clean Git control schema is invalid.")
        identities = (
            payload.get("source_project_identity"),
            payload.get("sandbox_identity"),
        )
        entries_payload = payload.get("entries")
        if (
            not isinstance(payload.get("generation"), str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["generation"]) is None
            or not isinstance(payload.get("source_project_root"), str)
            or not payload["source_project_root"]
            or not isinstance(payload.get("sandbox_repo"), str)
            or not payload["sandbox_repo"]
            or not all(
                isinstance(identity, list)
                and len(identity) == 2
                and all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                    for value in identity
                )
                for identity in identities
            )
            or not isinstance(payload.get("accepted_commit"), str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", payload["accepted_commit"])
            is None
            or not isinstance(payload.get("entry_count"), int)
            or isinstance(payload["entry_count"], bool)
            or not 0 < payload["entry_count"] <= _GIT_CONTROL_STATE_MAX_ENTRIES
            or not isinstance(entries_payload, dict)
            or len(entries_payload) != payload["entry_count"]
            or not isinstance(payload.get("authority_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["authority_digest"]) is None
        ):
            raise ValueError("Persisted clean Git control values are invalid.")
        entries: dict[str, _RawWorktreeEntry] = {}
        for key, raw_entry in entries_payload.items():
            if (
                not isinstance(key, str)
                or not key
                or key in entries
                or not (
                    key == "persisted-config-authority"
                    or key.startswith("common/")
                )
            ):
                raise ValueError("Persisted Git control path is invalid.")
            entries[key] = cls._raw_control_entry_from_payload(raw_entry)
        if cls._git_control_entries_digest(entries) != payload["authority_digest"]:
            raise ValueError("Persisted Git control digest is invalid.")

    @staticmethod
    def _validate_open_git_control_payload(payload: dict[str, Any]) -> None:
        if set(payload) != _GIT_CONTROL_OPEN_KEYS:
            raise ValueError("Persisted open Git control schema is invalid.")
        if (
            not isinstance(payload.get("generation"), str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["generation"]) is None
            or not isinstance(payload.get("token"), str)
            or re.fullmatch(r"[0-9a-f]{32}", payload["token"]) is None
            or not isinstance(payload.get("iteration"), int)
            or isinstance(payload["iteration"], bool)
            or payload["iteration"] < 0
            or not isinstance(payload.get("worktree"), str)
            or not payload["worktree"]
            or not isinstance(payload.get("clean_authority_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["clean_authority_digest"])
            is None
        ):
            raise ValueError("Persisted open Git control values are invalid.")

    def _persist_clean_git_control_state(self) -> None:
        accepted_head = self._git_output(
            ["rev-parse", f"refs/heads/{self.accepted_branch}^{{commit}}"],
            cwd=self.repo_root,
        )
        if accepted_head != self.accepted_commit:
            raise ValueError(
                "Sandbox accepted ref disagrees with the in-memory accepted head."
            )
        entries = self._snapshot_persistable_git_control()
        payload = self._clean_git_control_payload(entries)
        self._validate_clean_git_control_payload(payload)
        current = self._snapshot_persistable_git_control()
        if current != entries:
            raise ValueError("Sandbox Git control changed while publishing clean state.")
        final_accepted_head = self._git_output(
            ["rev-parse", f"refs/heads/{self.accepted_branch}^{{commit}}"],
            cwd=self.repo_root,
        )
        if final_accepted_head != self.accepted_commit:
            raise ValueError(
                "Sandbox accepted ref changed before the clean checkpoint."
            )
        # Publishing clean is the final commit point.  No fallible Git/control
        # verification may follow it: a caller-observed pre-commit failure must
        # leave the durable trial_open state available to reject rehydration.
        self._write_git_control_state(payload)
        self._clean_git_control_digest = payload["authority_digest"]
        self._git_control_state_verified = True

    def _require_clean_git_control_authority(
        self,
        *,
        force: bool,
    ) -> dict[str, Any]:
        self._require_compromise_marker_clear()
        payload = self._load_git_control_state()
        if payload.get("status") != "clean":
            raise ValueError(
                "Persisted Git control state is not clean; rebuild the workspace."
            )
        if (
            payload["source_project_root"] != str(self.source_project_root)
            or payload["sandbox_repo"] != str(self.repo_root)
        ):
            raise ValueError(
                "Persisted Git control source project root or sandbox workspace changed."
            )
        try:
            source_stat = os.lstat(self.source_project_root)
            sandbox_stat = os.lstat(self.repo_root)
        except OSError as exc:
            raise ValueError("Persisted Git control roots are unavailable.") from exc
        if payload["source_project_identity"] != [
            source_stat.st_dev,
            source_stat.st_ino,
        ] or payload["sandbox_identity"] != [
            sandbox_stat.st_dev,
            sandbox_stat.st_ino,
        ]:
            raise ValueError(
                "Persisted Git control source project identity or sandbox identity "
                "changed."
            )
        if force or not self._git_control_state_verified:
            current = self._snapshot_persistable_git_control()
            expected = {
                key: self._raw_control_entry_from_payload(value)
                for key, value in payload["entries"].items()
            }
            if current != expected:
                raise ValueError("Persisted clean Git control authority changed.")
        self._git_control_generation = payload["generation"]
        self._clean_git_control_digest = payload["authority_digest"]
        self._git_control_state_verified = True
        return payload

    def _begin_trial_git_control_state(
        self,
        *,
        iteration: int,
        worktree: Path,
    ) -> None:
        clean = self._require_clean_git_control_authority(force=True)
        token = uuid4().hex
        payload = {
            "schema_version": 1,
            "status": "trial_open",
            "generation": clean["generation"],
            "token": token,
            "iteration": iteration,
            "worktree": str(worktree),
            "clean_authority_digest": clean["authority_digest"],
        }
        self._write_git_control_state(payload)
        if self._load_git_control_state() != payload:
            raise ValueError("Persisted open Git control state changed after write.")
        self._active_trial_token = token
        self._active_trial_worktree = worktree
        self._git_control_state_verified = False

    def _replace_git_control_state_for_rebuild(self) -> None:
        state = self.git_control_state_path
        if not os.path.lexists(state):
            return
        try:
            state_stat = os.lstat(state)
            if stat.S_ISDIR(state_stat.st_mode) and not stat_is_filesystem_alias(
                state_stat
            ):
                raise ValueError(
                    "Git control state is an unexpected directory during rebuild."
                )
            state.unlink()
        except OSError as exc:
            raise ValueError("Git control state cannot be replaced for rebuild.") from exc
        if os.path.lexists(state):
            raise ValueError("Git control state survived the sandbox rebuild.")

    def open_existing(self) -> None:
        """Authenticate a quiescent durable workspace without resealing it."""

        if self._created:
            self._require_clean_git_control_authority(force=True)
            return
        if self._rebuilding:
            raise ValueError("Harness workspace rebuild is still in progress.")
        self._require_compromise_marker_clear()
        self._authenticate_sandbox_git_authority()
        self._git_config_authority = self._load_persisted_git_config_authority()
        try:
            current_config = self._snapshot_regular_file(
                self.repo_root / ".git" / "config"
            )
        except _PromotionConflict as exc:
            raise ValueError("Sandbox Git config cannot be authenticated.") from exc
        if current_config != self._git_config_authority:
            raise ValueError("Sandbox Git config authority changed.")
        clean = self._require_clean_git_control_authority(force=True)
        self._source_project_authority = (
            clean["source_project_root"],
            tuple(clean["source_project_identity"]),
        )
        self.accepted_commit = clean["accepted_commit"]
        self._created = True

    @contextmanager
    def _accepted_ref_cleanup_lock(self) -> Iterator[None]:
        """Hold Git's loose-ref lock across reachability proof and cleanup."""

        if self.accepted_branch != "accepted":
            raise ValueError("Accepted branch authority is not canonical.")
        common_dir = self._authenticate_sandbox_git_authority()
        lock_path = common_dir / "refs" / "heads" / "accepted.lock"
        if first_filesystem_alias_component(lock_path.parent) is not None:
            raise ValueError("Accepted ref lock parent contains an alias.")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(
                "Accepted ref is busy; candidate evidence must be retained."
            ) from exc
        try:
            os.write(descriptor, b"OmicsClaw accepted evidence cleanup\n")
            os.fsync(descriptor)
            locked = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        identity = (locked.st_dev, locked.st_ino)
        try:
            yield
        finally:
            try:
                current = os.lstat(lock_path)
                if (
                    stat_is_filesystem_alias(current)
                    or not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != identity
                ):
                    raise RuntimeError(
                        "Accepted ref cleanup lock identity changed."
                    )
                lock_path.unlink()
            except OSError as exc:
                raise RuntimeError(
                    "Accepted ref cleanup lock could not be released safely."
                ) from exc

    @staticmethod
    def _read_plain_git_control_file(
        path: Path,
        *,
        label: str,
        max_bytes: int = 4096,
    ) -> bytes:
        """Read one bounded Git control file without following aliases."""

        try:
            if first_filesystem_alias_component(path) is not None:
                raise ValueError(f"{label} contains an alias.")
            before = os.lstat(path)
            if (
                stat_is_filesystem_alias(before)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > max_bytes
            ):
                raise ValueError(f"{label} is not a bounded plain file.")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    chunk = os.read(descriptor, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
            finally:
                os.close(descriptor)
            after = os.lstat(path)
        except ValueError:
            raise
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{label} cannot be authenticated.") from exc
        identity = (before.st_dev, before.st_ino)
        if (
            len(content) > max_bytes
            or (opened.st_dev, opened.st_ino) != identity
            or (after.st_dev, after.st_ino) != identity
            or opened.st_nlink != 1
            or after.st_nlink != 1
            or opened.st_size != len(content)
            or after.st_size != len(content)
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise ValueError(f"{label} changed while being authenticated.")
        return content

    def _authenticate_linked_worktree_git_authority(self, worktree: Path) -> None:
        """Bind a linked worktree to the in-output sandbox common directory."""

        candidate = Path(os.path.abspath(worktree))
        common_dir = self._authenticate_sandbox_git_authority()
        try:
            relative = candidate.relative_to(self.worktrees_root)
            candidate_stat = os.lstat(candidate)
        except (OSError, ValueError) as exc:
            raise ValueError("Sandbox worktree authority is invalid.") from exc
        if (
            len(relative.parts) != 1
            or first_filesystem_alias_component(candidate) is not None
            or stat_is_filesystem_alias(candidate_stat)
            or not stat.S_ISDIR(candidate_stat.st_mode)
        ):
            raise ValueError("Sandbox worktree authority contains an alias.")

        dotgit = candidate / ".git"
        marker = self._read_plain_git_control_file(
            dotgit,
            label="Sandbox worktree .git marker",
        )
        try:
            marker_text = marker.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Sandbox worktree .git marker is not UTF-8.") from exc
        if not marker_text.startswith("gitdir: ") or not marker_text.endswith("\n"):
            raise ValueError("Sandbox worktree .git marker is invalid.")
        raw_gitdir = marker_text[len("gitdir: ") : -1]
        if "\n" in raw_gitdir or not Path(raw_gitdir).is_absolute():
            raise ValueError("Sandbox worktree gitdir identity is invalid.")
        gitdir = Path(os.path.abspath(raw_gitdir))
        expected_gitdir = common_dir / "worktrees" / candidate.name
        if gitdir != expected_gitdir:
            raise ValueError("Sandbox worktree gitdir is outside its authority.")

        commondir_bytes = self._read_plain_git_control_file(
            gitdir / "commondir",
            label="Sandbox worktree commondir marker",
        )
        backlink_bytes = self._read_plain_git_control_file(
            gitdir / "gitdir",
            label="Sandbox worktree backlink",
        )
        try:
            raw_commondir = commondir_bytes.decode("utf-8").rstrip("\n")
            raw_backlink = backlink_bytes.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError as exc:
            raise ValueError("Sandbox worktree authority is not UTF-8.") from exc
        if (
            "\n" in raw_commondir
            or "\n" in raw_backlink
            or Path(os.path.abspath(gitdir / raw_commondir)) != common_dir
            or Path(os.path.abspath(raw_backlink)) != dotgit
        ):
            raise ValueError("Sandbox worktree authority markers do not agree.")

    def _authenticate_candidate_worktree(
        self,
        *,
        iteration: int,
        worktree: Path,
        parent_commit: str,
    ) -> Path:
        candidate = self._canonical_trial_worktree_path(
            iteration=iteration,
            worktree=worktree,
        )
        self._authenticate_linked_worktree_git_authority(candidate)

        top_level = Path(
            self._git_output(["rev-parse", "--show-toplevel"], cwd=candidate)
        ).resolve()
        raw_common_dir = Path(
            self._git_output(["rev-parse", "--git-common-dir"], cwd=candidate)
        )
        common_dir = (
            raw_common_dir
            if raw_common_dir.is_absolute()
            else candidate / raw_common_dir
        ).resolve()
        expected_common_dir = (self.repo_root / ".git").resolve()
        try:
            head_commit = self._git_output(
                ["rev-parse", "HEAD^{commit}"],
                cwd=candidate,
            )
        except RuntimeError as exc:
            raise ValueError(
                "Accepted candidate worktree has no authenticated parent HEAD."
            ) from exc
        if (
            top_level != candidate
            or common_dir != expected_common_dir
            or head_commit != parent_commit
        ):
            raise ValueError(
                "Accepted candidate worktree does not belong to its sandbox parent."
            )
        return candidate

    def _canonical_trial_worktree_path(
        self,
        *,
        iteration: int,
        worktree: Path,
    ) -> Path:
        """Authenticate only the filesystem path, without consulting Git."""

        candidate = Path(os.path.abspath(Path(worktree).expanduser()))
        expected = self.worktrees_root / f"iter_{iteration:04d}"
        try:
            alias = first_filesystem_alias_component(candidate)
            candidate_stat = os.lstat(candidate)
        except (OSError, RuntimeError) as exc:
            raise ValueError("Accepted candidate worktree is unavailable.") from exc
        if (
            candidate != expected
            or alias is not None
            or stat_is_filesystem_alias(candidate_stat)
            or not stat.S_ISDIR(candidate_stat.st_mode)
        ):
            raise ValueError(
                "Accepted candidate worktree is not the canonical sandbox worktree."
            )
        return candidate

    def _require_worktree_status(
        self,
        worktree: Path,
        *,
        parent_commit: str,
        expected_status: bytes,
        expected_files: list[str],
        stage: str,
    ) -> None:
        inventory = self._require_exact_worktree_inventory(
            worktree,
            parent_commit=parent_commit,
            mutable_files=set(expected_files),
            stage=stage,
        )
        self._require_normal_candidate_index(
            worktree,
            expected_files=sorted(inventory),
            stage=stage,
        )
        raw = self._git_output_bytes(
            [
                *_DETERMINISTIC_WORKTREE_GIT_CONFIG,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
                "--ignore-submodules=none",
            ],
            cwd=worktree,
        )
        records = raw.split(b"\0")
        if records and records[-1] == b"":
            records.pop()
        observed: list[str] = []
        for record in records:
            if len(record) < 4 or record[:2] != expected_status or record[2:3] != b" ":
                raise ValueError(
                    f"Accepted candidate Git status is not clean {stage}."
                )
            try:
                rel_path = record[3:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "Accepted candidate Git status contains a non-UTF-8 path."
                ) from exc
            observed.append(
                self._canonical_repo_path(rel_path, label="candidate status path")
            )
            candidate_file = worktree.joinpath(*PurePosixPath(rel_path).parts)
            try:
                alias = first_filesystem_alias_component(candidate_file)
                candidate_stat = os.lstat(candidate_file)
            except (OSError, RuntimeError) as exc:
                raise ValueError(
                    "Accepted candidate file is unavailable or aliased."
                ) from exc
            if (
                alias is not None
                or stat_is_filesystem_alias(candidate_stat)
                or not stat.S_ISREG(candidate_stat.st_mode)
                or candidate_stat.st_nlink != 1
            ):
                raise ValueError(
                    "Accepted candidate file is not a plain single-link file."
                )
        if sorted(observed) != sorted(expected_files):
            raise ValueError(
                f"Accepted candidate Git delta is incomplete or contaminated {stage}."
            )

    def _snapshot_raw_worktree(
        self,
        worktree: Path,
    ) -> dict[str, _RawWorktreeEntry]:
        """Capture a no-follow raw snapshot of one candidate worktree."""

        return self._snapshot_plain_tree(
            worktree,
            excluded_root_names={".git"},
            label="candidate raw worktree",
        )

    def _snapshot_git_control(
        self,
        worktree: Path,
    ) -> dict[str, _RawWorktreeEntry]:
        """Capture common Git state plus the linked-worktree marker."""

        common_dir = self.repo_root / ".git"
        control = {
            f"common/{key}": value
            for key, value in self._snapshot_plain_tree(
                common_dir,
                excluded_root_names=set(),
                label="sandbox Git control",
            ).items()
        }
        marker = self._snapshot_regular_file(worktree / ".git")
        control["worktree-marker"] = self._raw_regular_entry(marker)
        persisted_config = self._snapshot_regular_file(
            self.git_config_authority_path
        )
        control["persisted-config-authority"] = self._raw_regular_entry(
            persisted_config
        )
        persisted_control_state = self._snapshot_regular_file(
            self.git_control_state_path
        )
        control["persisted-control-state"] = self._raw_regular_entry(
            persisted_control_state
        )
        return control

    def _verify_git_control_witness(
        self,
        worktree: Path,
        *,
        consume: bool,
    ) -> None:
        self._require_git_control_uncompromised()
        expected = self._trial_git_control_authorities.get(worktree)
        if expected is None:
            self._mark_git_control_compromised(worktree)
            raise ValueError("Candidate Git control witness is unavailable.")
        try:
            current = self._snapshot_git_control(worktree)
        except (OSError, RuntimeError, ValueError, _PromotionConflict) as exc:
            self._mark_git_control_compromised(worktree)
            raise ValueError("Candidate Git control cannot be authenticated.") from exc
        if current != expected:
            self._mark_git_control_compromised(worktree)
            raise ValueError("Candidate Git control changed after worktree creation.")
        if consume:
            self._trial_git_control_authorities.pop(worktree, None)

    def _mark_git_control_compromised(
        self,
        worktree: Path,
        *,
        reason: str = "git_control_drift",
    ) -> None:
        self._unsafe_worktree_controls.add(worktree)
        self._git_control_compromised = True
        if os.path.lexists(self.git_control_compromise_path):
            return
        try:
            atomic_write_owned_output_text(
                self.git_control_compromise_path,
                output_root=self.output_root,
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "status": "compromised",
                        "reason": reason,
                        "worktree": str(worktree),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    sort_keys=True,
                )
                + "\n",
                label="Git control compromise marker",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.critical(
                "Could not persist Git control compromise marker at %s: %s",
                self.git_control_compromise_path,
                exc,
            )
            raise RuntimeError(
                "Git control compromise marker could not be persisted."
            ) from exc

    def _clear_git_control_compromise_for_rebuild(self) -> None:
        marker = self.git_control_compromise_path
        if not os.path.lexists(marker):
            return
        try:
            marker_stat = os.lstat(marker)
        except OSError as exc:
            raise ValueError(
                "Git control compromise marker cannot be inspected."
            ) from exc
        if stat.S_ISDIR(marker_stat.st_mode) and not stat_is_filesystem_alias(
            marker_stat
        ):
            raise ValueError(
                "Git control compromise marker is an unexpected directory."
            )
        try:
            marker.unlink()
        except OSError as exc:
            raise ValueError(
                "Git control compromise marker cannot be cleared for rebuild."
            ) from exc
        if os.path.lexists(marker):
            raise ValueError(
                "Git control compromise marker survived the sandbox rebuild."
            )

    def _require_compromise_marker_clear(self) -> None:
        if os.path.lexists(self.git_control_compromise_path):
            self._git_control_compromised = True
        if self._git_control_compromised:
            raise ValueError(
                "Sandbox Git control authority is compromised; rebuild with create()."
            )

    def _require_git_control_uncompromised(self) -> None:
        if self._rebuilding:
            return
        self._require_compromise_marker_clear()
        if self._active_trial_token is not None:
            payload = self._load_git_control_state()
            if (
                payload.get("status") != "trial_open"
                or payload.get("generation") != self._git_control_generation
                or payload.get("token") != self._active_trial_token
                or payload.get("worktree") != str(self._active_trial_worktree)
                or payload.get("clean_authority_digest")
                != self._clean_git_control_digest
            ):
                self._git_control_compromised = True
                raise ValueError("Active trial Git control state changed.")
            return
        if self._created:
            payload = self._require_clean_git_control_authority(
                force=not self._git_control_state_verified,
            )
            # The durable clean checkpoint, not this mutable convenience member,
            # is the accepted-head authority between operations.
            self.accepted_commit = payload["accepted_commit"]

    def _snapshot_plain_tree(
        self,
        root: Path,
        *,
        excluded_root_names: set[str],
        label: str,
    ) -> dict[str, _RawWorktreeEntry]:
        snapshots: dict[str, _RawWorktreeEntry] = {}
        pending: list[tuple[Path, str]] = [(root, "")]
        while pending:
            directory, rel_directory = pending.pop()
            try:
                if first_filesystem_alias_component(directory) is not None:
                    raise ValueError(f"{label} directory contains an alias.")
                before = os.lstat(directory)
                if (
                    stat_is_filesystem_alias(before)
                    or not stat.S_ISDIR(before.st_mode)
                ):
                    raise ValueError(f"{label} directory is not plain.")
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
                for entry in entries:
                    if not rel_directory and entry.name in excluded_root_names:
                        continue
                    entry_path = Path(entry.path)
                    rel_path = (
                        f"{rel_directory}/{entry.name}"
                        if rel_directory
                        else entry.name
                    )
                    try:
                        rel_path.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        raise ValueError(f"{label} contains a non-UTF-8 path.") from exc
                    rel_path = self._canonical_repo_path(
                        rel_path,
                        label=f"{label} path",
                    )
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if stat_is_filesystem_alias(entry_stat):
                            raise ValueError(f"{label} contains an aliased directory.")
                        pending.append((entry_path, rel_path))
                    elif stat.S_ISREG(entry_stat.st_mode):
                        snapshots[rel_path] = self._raw_regular_entry(
                            self._snapshot_regular_file(entry_path)
                        )
                    elif stat.S_ISLNK(entry_stat.st_mode):
                        snapshots[rel_path] = self._snapshot_raw_symlink(entry_path)
                    else:
                        raise ValueError(f"{label} contains an unsupported entry.")
                after = os.lstat(directory)
            except ValueError:
                raise
            except _PromotionConflict as exc:
                raise ValueError(f"{label} file is unstable.") from exc
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"{label} cannot be authenticated.") from exc
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_mode != after.st_mode
                or before.st_nlink != after.st_nlink
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise ValueError(f"{label} directory changed while reading.")
            snapshots[rel_directory] = _RawWorktreeEntry(
                kind="directory",
                identity=(after.st_dev, after.st_ino),
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
                ctime_ns=after.st_ctime_ns,
                mode=stat.S_IMODE(after.st_mode),
                nlink=after.st_nlink,
                digest="",
            )
        return snapshots

    @staticmethod
    def _raw_regular_entry(snapshot: _FileSnapshot) -> _RawWorktreeEntry:
        return _RawWorktreeEntry(
            kind="regular",
            identity=snapshot.identity,
            size=snapshot.size,
            mtime_ns=snapshot.mtime_ns,
            ctime_ns=snapshot.ctime_ns,
            mode=snapshot.mode,
            nlink=1,
            digest=snapshot.digest,
        )

    def _snapshot_raw_symlink(self, path: Path) -> _RawWorktreeEntry:
        try:
            before = os.lstat(path)
            if not stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
                raise ValueError("Raw symbolic link is invalid.")
            target = os.fsencode(os.readlink(path))
            after = os.lstat(path)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("Raw symbolic link cannot be authenticated.") from exc
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_mode != after.st_mode
            or before.st_nlink != after.st_nlink
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise ValueError("Raw symbolic link changed while reading.")
        return _RawWorktreeEntry(
            kind="symlink",
            identity=(after.st_dev, after.st_ino),
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
            mode=stat.S_IMODE(after.st_mode),
            nlink=after.st_nlink,
            digest=self._sha256(target),
        )

    def _read_stable_regular_bytes(self, path: Path) -> bytes:
        try:
            before = self._snapshot_regular_file(path)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                after_open = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after = self._snapshot_regular_file(path)
        except (_PromotionConflict, OSError) as exc:
            raise ValueError("Editable baseline file cannot be read stably.") from exc
        content = b"".join(chunks)
        if (
            before != after
            or (opened.st_dev, opened.st_ino) != before.identity
            or (after_open.st_dev, after_open.st_ino) != before.identity
            or len(content) != before.size
            or self._sha256(content) != before.digest
        ):
            raise ValueError("Editable baseline file changed while reading.")
        return content

    def _require_exact_worktree_inventory(
        self,
        worktree: Path,
        *,
        parent_commit: str,
        mutable_files: set[str],
        stage: str,
    ) -> dict[str, tuple[str, str]]:
        """Bind raw candidate bytes and paths independently of Git filters."""

        seed = self._trial_worktree_authorities.get(worktree)
        if seed is None or seed[0] != parent_commit:
            raise ValueError(
                f"Accepted candidate raw seed is unavailable or stale {stage}."
            )
        initial = seed[1]
        object_format = self._git_output(
            ["rev-parse", "--show-object-format"],
            cwd=self.repo_root,
        )
        if object_format not in {"sha1", "sha256"}:
            raise ValueError("Sandbox Git object format is unsupported.")
        expected = self._commit_blob_inventory(
            parent_commit,
            object_format=object_format,
        )
        if not mutable_files.issubset(expected):
            raise ValueError(
                f"Accepted candidate mutable paths are absent from its parent {stage}."
            )

        expected_directories: set[str] = set()
        for rel_path in expected:
            parts = PurePosixPath(rel_path).parts
            for index in range(1, len(parts)):
                expected_directories.add(PurePosixPath(*parts[:index]).as_posix())
        seeded_directories = {
            rel_path for rel_path, entry in initial.items() if entry.kind == "directory"
        }
        seeded_files = set(initial) - seeded_directories
        if seeded_directories != expected_directories | {""} or seeded_files != set(
            expected
        ):
            raise ValueError(
                f"Accepted candidate raw seed paths disagree with its parent {stage}."
            )
        authenticated_seed = self._trial_seed_blob_inventories.get(worktree)
        if mutable_files and authenticated_seed != (parent_commit, expected):
            raise ValueError(
                f"Accepted candidate raw seed bytes were not authenticated {stage}."
            )
        for rel_path, (expected_mode, expected_oid) in expected.items():
            seeded_entry = initial[rel_path]
            seed_path = worktree.joinpath(*PurePosixPath(rel_path).parts)
            if expected_mode in {"100644", "100755"}:
                if (
                    seeded_entry.kind != "regular"
                    or seeded_entry.nlink != 1
                    or bool(seeded_entry.mode & stat.S_IXUSR)
                    != (expected_mode == "100755")
                    or seeded_entry.mode & 0o7000
                ):
                    raise ValueError(
                        f"Accepted candidate raw seed mode is invalid {stage}."
                    )
                raw_bytes = (
                    self._read_stable_regular_bytes(seed_path)
                    if not mutable_files
                    else None
                )
            elif expected_mode == "120000":
                if seeded_entry.kind != "symlink" or rel_path in mutable_files:
                    raise ValueError(
                        f"Accepted candidate raw symbolic-link seed is invalid {stage}."
                    )
                raw_bytes = None
                if not mutable_files:
                    before_link = self._snapshot_raw_symlink(seed_path)
                    try:
                        raw_bytes = os.fsencode(os.readlink(seed_path))
                    except OSError as exc:
                        raise ValueError(
                            f"Accepted candidate raw symbolic-link seed is unreadable {stage}."
                        ) from exc
                    after_link = self._snapshot_raw_symlink(seed_path)
                    if before_link != after_link or after_link != seeded_entry:
                        raise ValueError(
                            f"Accepted candidate raw symbolic-link seed changed {stage}."
                        )
            else:  # pragma: no cover - guarded by _commit_blob_inventory
                raise ValueError("Accepted candidate Git mode is unsupported.")
            if raw_bytes is not None and (
                self._git_blob_oid(raw_bytes, object_format=object_format)
                != expected_oid
                or self._sha256(raw_bytes) != seeded_entry.digest
            ):
                raise ValueError(
                    f"Accepted candidate raw seed bytes disagree with its parent {stage}."
                )

        if mutable_files:
            candidate = self._candidate_worktree_authorities.get(worktree)
            if (
                candidate is None
                or candidate.parent_commit != parent_commit
                or set(candidate.modified_files) != mutable_files
            ):
                raise ValueError(
                    f"Accepted candidate has no matching pre-trial witness {stage}."
                )
            witness = candidate.inventory
        else:
            witness = initial
        current = self._snapshot_raw_worktree(worktree)
        if current != witness:
            raise ValueError(
                f"Accepted candidate raw state changed after its trusted witness {stage}."
            )
        if not mutable_files:
            self._trial_seed_blob_inventories[worktree] = (
                parent_commit,
                dict(expected),
            )
        return expected

    @staticmethod
    def _git_blob_oid(content: bytes, *, object_format: str) -> str:
        if object_format not in {"sha1", "sha256"}:
            raise ValueError("Git object format is unsupported.")
        header = f"blob {len(content)}\0".encode("ascii")
        return hashlib.new(object_format, header + content).hexdigest()

    def _commit_blob_inventory(
        self,
        commit_hash: str,
        *,
        object_format: str,
    ) -> dict[str, tuple[str, str]]:
        raw = self._git_output_bytes(
            ["ls-tree", "-r", "-z", "--full-tree", commit_hash],
            cwd=self.repo_root,
        )
        records = raw.split(b"\0")
        if records and records[-1] == b"":
            records.pop()
        expected_oid_length = hashlib.new(object_format).digest_size * 2
        inventory: dict[str, tuple[str, str]] = {}
        for record in records:
            if b"\t" not in record:
                raise ValueError("Accepted candidate parent tree is malformed.")
            metadata, raw_path = record.split(b"\t", 1)
            fields = metadata.split()
            try:
                mode = fields[0].decode("ascii")
                kind = fields[1].decode("ascii")
                oid = fields[2].decode("ascii")
                rel_path = raw_path.decode("utf-8")
            except (IndexError, UnicodeDecodeError) as exc:
                raise ValueError(
                    "Accepted candidate parent tree is not canonical."
                ) from exc
            rel_path = self._canonical_repo_path(
                rel_path,
                label="candidate parent tree path",
            )
            if (
                len(fields) != 3
                or mode not in {"100644", "100755", "120000"}
                or kind != "blob"
                or len(oid) != expected_oid_length
                or any(char not in "0123456789abcdef" for char in oid)
                or rel_path in inventory
            ):
                raise ValueError(
                    "Accepted candidate parent tree has an unsupported entry."
                )
            inventory[rel_path] = (mode, oid)
        return inventory

    def _require_normal_candidate_index(
        self,
        worktree: Path,
        *,
        expected_files: list[str],
        stage: str,
    ) -> None:
        """Reject hidden index state before trusting candidate Git status."""

        inventories: list[list[str]] = []
        for flag in ("-v", "-f"):
            raw = self._git_output_bytes(
                [
                    *_DETERMINISTIC_WORKTREE_GIT_CONFIG,
                    "ls-files",
                    flag,
                    "-z",
                ],
                cwd=worktree,
            )
            records = raw.split(b"\0")
            if records and records[-1] == b"":
                records.pop()
            observed: list[str] = []
            for record in records:
                if len(record) < 3 or record[:2] != b"H ":
                    raise ValueError(
                        "Accepted candidate Git index contains hidden or "
                        f"non-normal state {stage}."
                    )
                try:
                    rel_path = record[2:].decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        "Accepted candidate Git index contains a non-UTF-8 path."
                    ) from exc
                observed.append(
                    self._canonical_repo_path(
                        rel_path,
                        label="candidate index path",
                    )
                )
            if len(observed) != len(set(observed)):
                raise ValueError(
                    f"Accepted candidate Git index is ambiguous {stage}."
                )
            inventories.append(observed)
        if inventories[0] != inventories[1]:
            raise ValueError(
                f"Accepted candidate Git index inventories disagree {stage}."
            )
        if sorted(inventories[0]) != expected_files:
            raise ValueError(
                f"Accepted candidate Git index paths disagree with its parent {stage}."
            )

    def _verify_patch_plan_bytes(
        self,
        *,
        parent_commit: str,
        candidate_treeish: str,
        patch_plan: dict[str, Any],
    ) -> None:
        self._validate_patch_plan_payload(patch_plan)
        for raw_diff in patch_plan["diffs"]:
            rel_path = raw_diff["file"]
            parent_text = self._patch_parent_text(
                self.read_file_from_commit(parent_commit, rel_path),
                rel_path=rel_path,
            )
            hunks = [
                Hunk(
                    old_code=raw_hunk["old_code"],
                    new_code=raw_hunk["new_code"],
                )
                for raw_hunk in raw_diff["hunks"]
            ]
            rendered = render_hunks(parent_text, hunks, rel_path=rel_path).encode(
                "utf-8"
            )
            candidate = self.read_file_from_commit(candidate_treeish, rel_path)
            if rendered != candidate:
                raise ValueError(
                    "Accepted PatchPlan hunks do not produce candidate bytes for "
                    f"{rel_path}."
                )

    @staticmethod
    def _patch_parent_text(content: bytes, *, rel_path: str) -> str:
        """Decode the same universal-newline text consumed by apply_patch."""

        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Accepted PatchPlan parent is not UTF-8 text: {rel_path}"
            ) from exc
        return text.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _build_commit_message_from_payload(
        iteration: int,
        patch_plan: dict[str, Any],
        *,
        hunk_count: int,
    ) -> str:
        diff_summary = (
            f"{len(patch_plan['diffs'])} file(s), {hunk_count} hunk(s)"
        )
        headline = patch_plan["description"].strip() or diff_summary
        headline = headline.splitlines()[0][:72]
        return f"Harness iteration {iteration:04d}: {headline}"

    def _capture_source_project_authority(
        self,
    ) -> tuple[str, tuple[int, int]]:
        """Capture one stable canonical source-root path and directory inode."""

        root = self.source_project_root
        try:
            canonical = root.resolve(strict=True)
            alias = first_filesystem_alias_component(root)
            before = os.lstat(root)
            after = os.lstat(root)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                "Accepted source project root cannot be authenticated."
            ) from exc
        before_identity = (before.st_dev, before.st_ino)
        after_identity = (after.st_dev, after.st_ino)
        if (
            canonical != root
            or Path(os.path.abspath(root)) != root
            or alias is not None
            or stat_is_filesystem_alias(before)
            or not stat.S_ISDIR(before.st_mode)
            or stat_is_filesystem_alias(after)
            or not stat.S_ISDIR(after.st_mode)
            or before_identity != after_identity
        ):
            raise ValueError(
                "Accepted source project root is not one stable canonical directory."
            )
        return str(root), before_identity

    def _require_source_project_authority(
        self,
    ) -> tuple[str, tuple[int, int]]:
        current = self._capture_source_project_authority()
        if (
            self._source_project_authority is not None
            and current != self._source_project_authority
        ):
            raise ValueError(
                "Accepted source project identity changed after workspace creation."
            )
        return current

    def _validate_record_provenance(
        self,
        record: AcceptedPatchRecord,
    ) -> None:
        """Require canonical immutable provenance before publishing evidence."""

        if _SOURCE_COMMIT_RE.fullmatch(record.source_project_commit) is None:
            raise ValueError("Accepted source project commit identity is invalid.")
        current_root, current_identity = self._require_source_project_authority()
        if record.source_project_root != current_root:
            raise ValueError(
                "Accepted source project root does not match this workspace."
            )
        if (
            not isinstance(record.source_project_identity, tuple)
            or len(record.source_project_identity) != 2
            or not all(
                isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
                for item in record.source_project_identity
            )
            or record.source_project_identity != current_identity
        ):
            raise ValueError(
                "Accepted source project identity does not match this workspace."
            )
        try:
            parsed_timestamp = datetime.fromisoformat(record.timestamp)
        except ValueError as exc:
            raise ValueError("Accepted evidence timestamp is invalid.") from exc
        if (
            parsed_timestamp.tzinfo is None
            or parsed_timestamp.utcoffset() != timezone.utc.utcoffset(None)
            or parsed_timestamp.isoformat() != record.timestamp
        ):
            raise ValueError("Accepted evidence timestamp is not canonical UTC.")

    @staticmethod
    def _accepted_evidence_digest(
        record: AcceptedPatchRecord,
        patch_plan: dict[str, Any],
    ) -> str:
        """Digest every non-circular manifest field plus the exact PatchPlan."""

        authority_record = record.to_dict()
        for derived_key in ("commit_hash", "artifact_path", "manifest_path"):
            authority_record.pop(derived_key)
        canonical_evidence = json.dumps(
            {"record": authority_record, "patch_plan": patch_plan},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical_evidence).hexdigest()

    @classmethod
    def _build_commit_message_with_evidence_digest(
        cls,
        iteration: int,
        patch_plan: dict[str, Any],
        record: AcceptedPatchRecord,
        *,
        hunk_count: int,
    ) -> str:
        subject = cls._build_commit_message_from_payload(
            iteration,
            patch_plan,
            hunk_count=hunk_count,
        )
        digest = cls._accepted_evidence_digest(record, patch_plan)
        return f"{subject}\n\n{_ACCEPTED_EVIDENCE_TRAILER}: {digest}"

    def _read_durable_accepted_bytes(
        self,
        path: Path,
        *,
        label: str,
        max_bytes: int,
    ) -> bytes:
        """Read one accepted artifact without following mutable aliases."""

        candidate = Path(path)
        try:
            candidate.relative_to(self.accepted_artifacts_root)
        except ValueError as exc:
            raise ValueError(
                f"Durable accepted {label} is outside the artifact root."
            ) from exc
        try:
            alias = first_filesystem_alias_component(candidate)
            before = os.lstat(candidate)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"Durable accepted {label} is unavailable."
            ) from exc
        if (
            alias is not None
            or stat_is_filesystem_alias(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > max_bytes
        ):
            raise ValueError(
                f"Durable accepted {label} is not a bounded regular single-link file."
            )

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(candidate, flags)
        except OSError as exc:
            raise ValueError(
                f"Durable accepted {label} cannot be opened."
            ) from exc
        try:
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or identity != (before.st_dev, before.st_ino)
            ):
                raise ValueError(
                    f"Durable accepted {label} changed while opening."
                )
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after_open = os.fstat(descriptor)
            if (
                len(content) > max_bytes
                or (after_open.st_dev, after_open.st_ino) != identity
                or after_open.st_nlink != 1
                or after_open.st_size != opened.st_size
                or after_open.st_mtime_ns != opened.st_mtime_ns
                or after_open.st_ctime_ns != opened.st_ctime_ns
            ):
                raise ValueError(
                    f"Durable accepted {label} changed while reading."
                )
        finally:
            os.close(descriptor)

        try:
            after = os.lstat(candidate)
        except OSError as exc:
            raise ValueError(
                f"Durable accepted {label} changed after reading."
            ) from exc
        if (
            stat_is_filesystem_alias(after)
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != identity
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ValueError(
                f"Durable accepted {label} changed after reading."
            )
        return content

    def _changed_regular_files(self, parent_commit: str, commit_hash: str) -> list[str]:
        """Return canonical content-only regular-file modifications."""

        raw = self._git_output_bytes(
            [
                "diff-tree",
                "--no-commit-id",
                "--raw",
                "--no-renames",
                "-r",
                "-z",
                parent_commit,
                commit_hash,
                "--",
            ],
            cwd=self.repo_root,
        )
        fields = raw.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise ValueError("Durable accepted Git delta is malformed.")

        changed: list[str] = []
        for index in range(0, len(fields), 2):
            metadata = fields[index].split()
            try:
                rel_path = fields[index + 1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "Durable accepted Git path is not UTF-8."
                ) from exc
            if (
                len(metadata) != 5
                or not metadata[0].startswith(b":")
                or metadata[4] != b"M"
                or metadata[0][1:] != metadata[1]
                or metadata[0][1:] not in _REGULAR_GIT_MODES
                or metadata[2] == metadata[3]
            ):
                raise ValueError(
                    "Durable accepted state contains a non-content or mode-changing "
                    "Git delta."
                )
            # Reuse the source-tree path contract now, before any journal exists.
            self._source_target(rel_path)
            changed.append(rel_path)
        if len(changed) != len(set(changed)):
            raise ValueError("Durable accepted Git delta contains duplicate paths.")
        return sorted(changed)

    def _regular_file_mode_from_commit(
        self,
        commit_hash: str,
        rel_path: str,
    ) -> int:
        """Return the bounded POSIX mode authenticated by one Git tree entry."""

        raw = self._git_output_bytes(
            ["ls-tree", "-z", commit_hash, "--", rel_path],
            cwd=self.repo_root,
        )
        records = raw.split(b"\0")
        if records and records[-1] == b"":
            records.pop()
        if len(records) != 1 or b"\t" not in records[0]:
            raise ValueError("Durable accepted Git tree entry is malformed.")
        metadata, raw_path = records[0].split(b"\t", 1)
        try:
            tree_path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Durable accepted Git tree path is not UTF-8.") from exc
        fields = metadata.split()
        if (
            len(fields) != 3
            or fields[0] not in _REGULAR_GIT_MODES
            or fields[1] != b"blob"
            or tree_path != rel_path
        ):
            raise ValueError(
                "Durable accepted Git tree entry is not a supported regular file."
            )
        return _GIT_MODE_TO_POSIX_MODE[fields[0]]

    def _load_durable_accepted_record(
        self,
        *,
        commit_hash: str,
        parent_commit: str,
        expected_patch_plan: dict[str, Any] | None = None,
    ) -> AcceptedPatchRecord:
        """Authenticate one accepted commit against its manifest and patch."""

        subject = self._git_output(
            ["show", "-s", "--format=%s", commit_hash],
            cwd=self.repo_root,
        )
        match = _ACCEPTED_COMMIT_SUBJECT_RE.fullmatch(subject)
        if match is None:
            raise ValueError("Durable accepted commit subject is not canonical.")
        iteration = int(match.group(1))
        stem = f"iter_{iteration:04d}_{commit_hash[:12]}"
        expected_artifact = self.accepted_artifacts_root / f"{stem}.patch"
        expected_manifest = self.accepted_artifacts_root / f"{stem}.json"

        raw_manifest = self._read_durable_accepted_bytes(
            expected_manifest,
            label="manifest",
            max_bytes=_ACCEPTED_MANIFEST_MAX_BYTES,
        )
        try:
            payload = json.loads(
                raw_manifest.decode("utf-8"),
                object_pairs_hook=_json_object_without_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Durable accepted manifest is invalid JSON.") from exc
        record_keys = set(AcceptedPatchRecord.__dataclass_fields__)
        if (
            not isinstance(payload, dict)
            or set(payload) != record_keys | {"patch_plan"}
            or not isinstance(payload.get("patch_plan"), dict)
        ):
            raise ValueError("Durable accepted manifest schema is invalid.")
        raw_record = {key: payload[key] for key in record_keys}
        try:
            record = AcceptedPatchRecord.from_dict(raw_record)
        except (TypeError, ValueError) as exc:
            raise ValueError("Durable accepted manifest record is invalid.") from exc
        if record.to_dict() != raw_record:
            raise ValueError("Durable accepted manifest record is not canonical.")
        self._validate_record_provenance(record)

        patch_plan = payload["patch_plan"]
        plan_files, hunk_count = self._validate_patch_plan_payload(patch_plan)
        if expected_patch_plan is not None and patch_plan != expected_patch_plan:
            raise ValueError(
                "Durable accepted PatchPlan is not the supplied frozen plan."
            )

        expected_files = self._changed_regular_files(parent_commit, commit_hash)
        expected_summary = (
            f"{len(patch_plan['diffs'])} file(s), {hunk_count} hunk(s)"
        )
        expected_subject = self._build_commit_message_from_payload(
            iteration,
            patch_plan,
            hunk_count=hunk_count,
        )
        expected_message = self._build_commit_message_with_evidence_digest(
            iteration,
            patch_plan,
            record,
            hunk_count=hunk_count,
        )
        commit_message = self._git_output(
            ["show", "-s", "--format=%B", commit_hash],
            cwd=self.repo_root,
        )
        expected_worktree = self.worktrees_root / f"iter_{iteration:04d}"
        if (
            iteration <= 0
            or subject != expected_subject
            or commit_message != expected_message
            or record.iteration != iteration
            or record.commit_hash != commit_hash
            or record.parent_commit != parent_commit
            or Path(record.artifact_path) != expected_artifact
            or Path(record.manifest_path) != expected_manifest
            or record.sandbox_repo != str(self.repo_root)
            or Path(record.sandbox_worktree) != expected_worktree
            or len(record.modified_files) != len(set(record.modified_files))
            or record.modified_files != plan_files
            or sorted(record.modified_files) != expected_files
            or record.reasoning != patch_plan["reasoning"]
            or record.diff_summary != expected_summary
            or record.description != patch_plan["description"]
            or record.expected_improvements
            != patch_plan["expected_improvements"]
            or record.rollback_conditions != patch_plan["rollback_conditions"]
        ):
            raise ValueError(
                "Durable accepted manifest does not match its Git commit."
            )

        self._verify_patch_plan_bytes(
            parent_commit=parent_commit,
            candidate_treeish=commit_hash,
            patch_plan=patch_plan,
        )

        artifact = self._read_durable_accepted_bytes(
            expected_artifact,
            label="patch artifact",
            max_bytes=_ACCEPTED_PATCH_MAX_BYTES,
        )
        expected_patch = self._git_output(
            ["format-patch", "--stdout", "-1", commit_hash],
            cwd=self.repo_root,
        ).encode("utf-8")
        if artifact != expected_patch:
            raise ValueError(
                "Durable accepted patch artifact does not match its Git commit."
            )
        return record

    def _authenticate_accepted_chain(
        self,
        *,
        accepted_commit: str,
        supplied_record: AcceptedPatchRecord | None = None,
        supplied_patch_plan: dict[str, Any] | None = None,
    ) -> tuple[str, list[str], AcceptedPatchRecord]:
        """Authenticate every accepted commit and derive cumulative file authority."""

        roots = self._git_output(
            ["rev-list", "--max-parents=0", accepted_commit],
            cwd=self.repo_root,
        ).splitlines()
        if len(roots) != 1:
            raise ValueError("Durable accepted Git history has no unique baseline.")
        baseline_commit = roots[0]
        commits = self._git_output(
            ["rev-list", "--reverse", f"{baseline_commit}..{accepted_commit}"],
            cwd=self.repo_root,
        ).splitlines()
        if not commits or commits[-1] != accepted_commit:
            raise ValueError("Durable accepted Git history is incomplete.")

        parent_commit = baseline_commit
        final_record: AcceptedPatchRecord | None = None
        source_project_commit: str | None = None
        source_project_authority: tuple[str, tuple[int, int]] | None = None
        for commit_hash in commits:
            ancestry = self._git_output(
                ["rev-list", "--parents", "-n", "1", commit_hash],
                cwd=self.repo_root,
            ).split()
            if ancestry != [commit_hash, parent_commit]:
                raise ValueError("Durable accepted Git history is not linear.")
            final_record = self._load_durable_accepted_record(
                commit_hash=commit_hash,
                parent_commit=parent_commit,
                expected_patch_plan=(
                    supplied_patch_plan
                    if commit_hash == accepted_commit
                    else None
                ),
            )
            if source_project_commit is None:
                source_project_commit = final_record.source_project_commit
            elif final_record.source_project_commit != source_project_commit:
                raise ValueError(
                    "Durable accepted chain has inconsistent source project identity."
                )
            record_source_authority = (
                final_record.source_project_root,
                final_record.source_project_identity,
            )
            if source_project_authority is None:
                source_project_authority = record_source_authority
            elif record_source_authority != source_project_authority:
                raise ValueError(
                    "Durable accepted chain has inconsistent source root authority."
                )
            parent_commit = commit_hash

        assert final_record is not None
        if (
            supplied_record is not None
            and final_record.to_dict() != supplied_record.to_dict()
        ):
            raise ValueError(
                "Supplied accepted patch is not the exact durable accepted record."
            )
        return (
            baseline_commit,
            self._changed_regular_files(baseline_commit, accepted_commit),
            final_record,
        )

    def durable_accepted_head_record(self) -> AcceptedPatchRecord:
        """Load the accepted-head record exclusively from durable sandbox state."""

        clean = self._require_clean_git_control_authority(force=True)
        if self.accepted_commit and clean["accepted_commit"] != self.accepted_commit:
            raise ValueError("Durable accepted head disagrees with clean Git authority.")
        self.accepted_commit = clean["accepted_commit"]
        accepted_head = self._git_output(
            ["rev-parse", f"refs/heads/{self.accepted_branch}^{{commit}}"],
            cwd=self.repo_root,
        )
        if accepted_head != clean["accepted_commit"]:
            raise ValueError(
                "Durable accepted ref disagrees with clean Git authority."
            )
        _baseline, _files, record = self._authenticate_accepted_chain(
            accepted_commit=accepted_head,
        )
        return record

    def _accepted_state_for_promotion(
        self,
        accepted_patch: AcceptedPatchRecord,
    ) -> tuple[str, str, list[str]]:
        """Authenticate the caller record and return durable promotion authority."""

        accepted_commit = accepted_patch.commit_hash.strip()
        if not accepted_commit:
            raise ValueError("Accepted patch record has no commit hash.")
        resolved_commit = self._git_output(
            ["rev-parse", f"{accepted_commit}^{{commit}}"],
            cwd=self.repo_root,
        )
        if resolved_commit != accepted_commit:
            raise ValueError("Accepted patch record does not identify an exact commit.")
        accepted_head = self._git_output(
            ["rev-parse", f"refs/heads/{self.accepted_branch}^{{commit}}"],
            cwd=self.repo_root,
        )
        if accepted_head != accepted_commit:
            raise ValueError(
                "Accepted patch record is not the durable accepted branch head."
            )
        baseline_commit, unique_files, _durable_record = (
            self._authenticate_accepted_chain(
                accepted_commit=accepted_commit,
                supplied_record=accepted_patch,
            )
        )
        return accepted_commit, baseline_commit, unique_files

    def _require_source_tracked_state_matches_baseline(
        self,
        *,
        accepted_patch: AcceptedPatchRecord,
        baseline_commit: str,
        modified_files: list[str],
    ) -> None:
        """Require every tracked runtime byte to match the evaluated baseline."""

        mutable = set(modified_files)
        source_commit, _source_index, source_entries = (
            self._capture_tracked_source_snapshot(
                unobserved_paths=mutable,
            )
        )
        if source_commit != accepted_patch.source_project_commit:
            raise ValueError("Source Git HEAD changed after baseline evaluation.")
        object_format = self._git_output(
            ["rev-parse", "--show-object-format"],
            cwd=self.repo_root,
        )
        expected = self._commit_blob_inventory(
            baseline_commit,
            object_format=object_format,
        )
        current: dict[str, tuple[str, str]] = {}
        for entry in source_entries:
            oid = self._git_blob_oid(
                entry.content,
                object_format=object_format,
            )
            current[entry.rel_path] = (entry.git_mode, oid)
        for rel_path in mutable:
            if rel_path not in expected:
                raise ValueError(
                    "Promotion target is absent from the evaluated baseline."
                )
            current[rel_path] = expected[rel_path]
        if set(current) != set(expected) or any(
            current[rel_path] != expected[rel_path]
            for rel_path in expected
            if rel_path not in mutable
        ):
            raise ValueError(
                "Source tracked state changed after baseline evaluation; "
                "promotion requires a fresh run."
            )

    @staticmethod
    def _journal_identity(value: object) -> tuple[int, int] | None:
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(
                isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
                for item in value
            )
        ):
            return None
        return value[0], value[1]

    @staticmethod
    def _journal_mode(value: object) -> int | None:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 0o7777
        ):
            return None
        return value

    @classmethod
    def _journal_parent_chain(
        cls,
        value: object,
        *,
        expected_length: int,
    ) -> tuple[tuple[int, int], ...] | None:
        if not isinstance(value, list) or len(value) != expected_length:
            return None
        identities: list[tuple[int, int]] = []
        for raw_identity in value:
            identity = cls._journal_identity(raw_identity)
            if identity is None:
                return None
            identities.append(identity)
        return tuple(identities)

    def _validated_applied_promotion(
        self,
        accepted_patch: AcceptedPatchRecord,
    ) -> PromotionResult | None:
        """Return an idempotent result only for an exact durable applied state."""

        payload = self._read_promotion_journal()
        if payload is None or payload.get("status") != "applied":
            return None
        if set(payload) != _APPLIED_JOURNAL_KEYS:
            raise RuntimeError("Applied promotion journal has an unknown schema.")

        accepted_commit, baseline_commit, unique_files = (
            self._accepted_state_for_promotion(accepted_patch)
        )
        transaction_id = payload.get("transaction_id")
        if (
            payload.get("schema_version") != 2
            or not isinstance(transaction_id, str)
            or len(transaction_id) != 32
            or any(char not in "0123456789abcdef" for char in transaction_id)
            or payload.get("source_project_root")
            != str(self.source_project_root)
            or payload.get("sandbox_repo") != str(self.repo_root)
            or payload.get("baseline_commit") != baseline_commit
            or payload.get("accepted_commit") != accepted_commit
            or payload.get("files") != unique_files
            or payload.get("applied_files") != unique_files
            or payload.get("blocked_files") != []
            or not isinstance(payload.get("timestamp"), str)
            or not payload["timestamp"]
            or payload.get("message") != _APPLIED_PROMOTION_MESSAGE
        ):
            raise RuntimeError("Applied promotion journal identity is invalid.")

        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) != len(
            unique_files
        ):
            raise RuntimeError("Applied promotion journal entries are invalid.")

        validated_entries: list[
            tuple[_PromotionEntry, _FileSnapshot, _FileSnapshot | None]
        ] = []
        for index, (rel_path, raw_entry) in enumerate(
            zip(unique_files, raw_entries, strict=True)
        ):
            if not isinstance(raw_entry, dict) or set(raw_entry) != (
                _PROMOTION_ENTRY_KEYS
            ):
                raise RuntimeError("Applied promotion journal entry is malformed.")
            target = self.source_project_root.joinpath(
                *PurePosixPath(rel_path).parts
            )
            stage = target.with_name(
                f".{target.name}.omicsclaw-{transaction_id}-{index}.new"
            )
            backup = target.with_name(
                f".{target.name}.omicsclaw-{transaction_id}-{index}.bak"
            )
            expected_identity = self._journal_identity(
                raw_entry.get("expected_identity")
            )
            expected_mode = self._journal_mode(raw_entry.get("expected_mode"))
            parent_chain_identities = self._journal_parent_chain(
                raw_entry.get("parent_chain_identities"),
                expected_length=len(PurePosixPath(rel_path).parts),
            )
            stage_identity = self._journal_identity(
                raw_entry.get("stage_identity")
            )
            installed_identity = self._journal_identity(
                raw_entry.get("installed_identity")
            )
            try:
                baseline_git_mode = self._regular_file_mode_from_commit(
                    baseline_commit,
                    rel_path,
                )
                accepted_git_mode = self._regular_file_mode_from_commit(
                    accepted_commit,
                    rel_path,
                )
            except ValueError as exc:
                raise RuntimeError(
                    "Applied promotion journal entry is invalid."
                ) from exc
            expected_digest = self._sha256(
                self.read_file_from_commit(baseline_commit, rel_path)
            )
            accepted_digest = self._sha256(
                self.read_file_from_commit(accepted_commit, rel_path)
            )
            if (
                raw_entry.get("path") != rel_path
                or raw_entry.get("stage_name") != stage.name
                or raw_entry.get("backup_name") != backup.name
                or expected_identity is None
                or expected_mode is None
                or parent_chain_identities is None
                or stage_identity is None
                or installed_identity is None
                or stage_identity != installed_identity
                or accepted_git_mode != baseline_git_mode
                or bool(expected_mode & 0o111)
                != bool(baseline_git_mode & 0o111)
                or raw_entry.get("expected_digest") != expected_digest
                or raw_entry.get("accepted_digest") != accepted_digest
                or raw_entry.get("phase") != "applied"
            ):
                raise RuntimeError("Applied promotion journal entry is invalid.")

            entry = _PromotionEntry(
                rel_path=rel_path,
                target=target,
                stage=stage,
                backup=backup,
                expected=_FileSnapshot(
                    identity=expected_identity,
                    size=0,
                    mtime_ns=0,
                    ctime_ns=0,
                    mode=expected_mode,
                    digest=expected_digest,
                ),
                accepted_digest=accepted_digest,
                parent_chain_identities=parent_chain_identities,
                phase="applied",
                stage_identity=stage_identity,
                installed_identity=installed_identity,
            )
            if os.path.lexists(stage):
                current = self._snapshot_interrupted_install_link(entry)
            else:
                try:
                    current = self._snapshot_installed_target(entry)
                except _PromotionConflict as exc:
                    raise RuntimeError(
                        f"Applied promotion source state drifted: {rel_path}"
                    ) from exc
            if (
                current.identity != installed_identity
                or current.digest != accepted_digest
                or current.mode != expected_mode
            ):
                raise RuntimeError(
                    f"Applied promotion source state drifted: {rel_path}"
                )

            backup_snapshot: _FileSnapshot | None = None
            if os.path.lexists(backup):
                backup_snapshot = self._snapshot_regular_file(backup)
                if (
                    backup_snapshot.identity != expected_identity
                    or backup_snapshot.digest != expected_digest
                    or backup_snapshot.mode != expected_mode
                ):
                    raise RuntimeError(
                        f"Applied promotion backup state drifted: {rel_path}"
                    )
            validated_entries.append((entry, current, backup_snapshot))

        # A process can stop after the durable applied write but before private
        # transaction names are removed.  Validate the *entire* journal and
        # source state first, then clean only the exact canonical inode/digest
        # remnants bound above.  Any drift leaves every remaining name intact.
        try:
            self._require_source_tracked_state_matches_baseline(
                accepted_patch=accepted_patch,
                baseline_commit=baseline_commit,
                modified_files=unique_files,
            )
        except ValueError as exc:
            payload["status"] = "recovery_required"
            payload["message"] = (
                "Applied promotion cleanup is blocked by tracked-source "
                f"drift: {exc}"
            )
            payload["blocked_files"] = []
            self._write_promotion_journal(payload)
            return PromotionResult(
                status="recovery_required",
                message=payload["message"],
                journal_path=str(self.promotion_journal_path),
            )
        for entry, installed, backup_snapshot in validated_entries:
            if os.path.lexists(entry.stage):
                linked = self._snapshot_interrupted_install_link(entry)
                if linked != installed:
                    raise RuntimeError(
                        f"Applied promotion stage state drifted: {entry.rel_path}"
                    )
                self._validate_source_parent_chain(entry)
                entry.stage.unlink()
                collapsed = self._snapshot_regular_file(entry.target)
                if (
                    collapsed.identity != installed.identity
                    or collapsed.digest != entry.accepted_digest
                ):
                    raise RuntimeError(
                        f"Applied promotion target changed: {entry.rel_path}"
                    )
            if backup_snapshot is not None:
                self._snapshot_installed_target(entry)
                current_backup = self._snapshot_regular_file(entry.backup)
                if current_backup != backup_snapshot:
                    raise RuntimeError(
                        f"Applied promotion backup changed: {entry.rel_path}"
                    )
                self._validate_source_parent_chain(entry)
                entry.backup.unlink()

        for entry, _installed, _backup_snapshot in validated_entries:
            self._snapshot_installed_target(entry)
        try:
            self._require_source_tracked_state_matches_baseline(
                accepted_patch=accepted_patch,
                baseline_commit=baseline_commit,
                modified_files=unique_files,
            )
        except ValueError as exc:
            payload["status"] = "recovery_required"
            payload["message"] = (
                "Applied promotion cleanup finished, but final tracked-source "
                f"verification failed: {exc}"
            )
            payload["blocked_files"] = []
            self._write_promotion_journal(payload)
            return PromotionResult(
                status="recovery_required",
                message=payload["message"],
                journal_path=str(self.promotion_journal_path),
            )

        return PromotionResult(
            status="applied",
            message=_APPLIED_PROMOTION_MESSAGE,
            promoted_files=list(unique_files),
            journal_path=str(self.promotion_journal_path),
        )

    def promote_accepted_state(
        self,
        *,
        accepted_patch: AcceptedPatchRecord,
    ) -> PromotionResult:
        """Promote one explicitly accepted snapshot back to the source worktree.

        The complete accepted chain, promotable file set, final record, and patch
        artifacts are re-derived from durable Git and accepted-manifest state,
        never from a caller's mutable file list or workspace cache. Each source
        entry is atomically moved to a private backup and compared with its
        read-stable preflight identity/digest before accepted bytes are installed
        with a no-clobber hard-link operation.
        """
        clean = self._require_clean_git_control_authority(force=True)
        if clean["accepted_commit"] != accepted_patch.commit_hash:
            raise ValueError(
                "Promotion record is not the clean accepted branch authority."
            )
        completed = self._validated_applied_promotion(accepted_patch)
        if completed is not None:
            return completed

        recovery = self._recover_interrupted_promotion(accepted_patch)
        if recovery is not None:
            return recovery

        accepted_commit, baseline_commit, unique_files = (
            self._accepted_state_for_promotion(accepted_patch)
        )
        self._require_source_tracked_state_matches_baseline(
            accepted_patch=accepted_patch,
            baseline_commit=baseline_commit,
            modified_files=unique_files,
        )
        # This member is a convenience for summaries only. Promotion authority
        # above came from the authenticated Git root, not prior process memory.
        self.baseline_commit = baseline_commit
        durable_modes: dict[str, int] = {}
        for rel_path in unique_files:
            baseline_mode = self._regular_file_mode_from_commit(
                baseline_commit,
                rel_path,
            )
            accepted_mode = self._regular_file_mode_from_commit(
                accepted_commit,
                rel_path,
            )
            if accepted_mode != baseline_mode:
                raise ValueError(
                    "Durable accepted state contains a mode-changing Git entry."
                )
            durable_modes[rel_path] = baseline_mode
        transaction_id = uuid4().hex
        journal: dict[str, Any] = {
            "schema_version": 2,
            "status": "not_needed" if not unique_files else "preparing",
            "transaction_id": transaction_id,
            "source_project_root": str(self.source_project_root),
            "sandbox_repo": str(self.repo_root),
            "baseline_commit": baseline_commit,
            "accepted_commit": accepted_commit,
            "files": unique_files,
            "entries": [],
            "applied_files": [],
            "blocked_files": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._write_promotion_journal(journal)

        if not unique_files:
            journal["message"] = "No accepted files to promote."
            self._write_promotion_journal(journal)
            return PromotionResult(
                status="not_needed",
                message=journal["message"],
                journal_path=str(self.promotion_journal_path),
            )

        entries: list[_PromotionEntry] = []
        stage_payloads: list[tuple[_PromotionEntry, bytes]] = []
        blocked: list[str] = []
        try:
            for index, rel_path in enumerate(unique_files):
                target = self._source_target(rel_path)
                parent_chain_identities = self._snapshot_source_parent_chain(
                    rel_path
                )
                baseline_bytes = self.read_file_from_commit(
                    baseline_commit,
                    rel_path,
                )
                snapshot = self._snapshot_regular_file(target)
                if (
                    snapshot.digest != self._sha256(baseline_bytes)
                    or bool(snapshot.mode & 0o111)
                    != bool(durable_modes[rel_path] & 0o111)
                ):
                    blocked.append(rel_path)
                    continue

                # Read accepted bytes only after the stable source snapshot so
                # the later atomic capture detects modifications in this window.
                accepted_bytes = self.read_file_from_commit(
                    accepted_commit,
                    rel_path,
                )
                stage = target.with_name(
                    f".{target.name}.omicsclaw-{transaction_id}-{index}.new"
                )
                backup = target.with_name(
                    f".{target.name}.omicsclaw-{transaction_id}-{index}.bak"
                )
                entry = _PromotionEntry(
                    rel_path=rel_path,
                    target=target,
                    stage=stage,
                    backup=backup,
                    expected=snapshot,
                    accepted_digest=self._sha256(accepted_bytes),
                    parent_chain_identities=parent_chain_identities,
                    phase="staging",
                )
                entries.append(entry)
                stage_payloads.append((entry, accepted_bytes))
        except _PromotionConflict as exc:
            self._cleanup_promotion_entries(entries)
            journal["status"] = "blocked"
            journal["blocked_files"] = [rel_path]
            journal["message"] = str(exc)
            self._write_promotion_journal(journal)
            return PromotionResult(
                status="blocked",
                message=str(exc),
                blocked_files=[rel_path],
                journal_path=str(self.promotion_journal_path),
            )
        except Exception:
            self._cleanup_promotion_entries(entries)
            raise

        if blocked:
            self._cleanup_promotion_entries(entries)
            journal["status"] = "blocked"
            journal["blocked_files"] = blocked
            journal["message"] = (
                "Source files changed after the harness snapshot was created; "
                "manual promotion is required."
            )
            self._write_promotion_journal(journal)
            return PromotionResult(
                status="blocked",
                message=journal["message"],
                blocked_files=blocked,
                journal_path=str(self.promotion_journal_path),
            )

        # Declare *all* canonical transaction-owned names durably before any
        # stage can exist.  A crash on either side of a later creation is then
        # recoverable from the journal without scanning or deleting an unbound
        # dotfile in the user's source tree.
        journal["entries"] = [entry.to_journal_dict() for entry in entries]
        try:
            self._write_promotion_journal(journal)
            for entry, accepted_bytes in stage_payloads:
                self._write_exclusive_stage(entry, accepted_bytes)
                entry.phase = "prepared"
                journal["entries"] = [
                    item.to_journal_dict() for item in entries
                ]
                self._write_promotion_journal(journal)
        except Exception:
            self._cleanup_promotion_entries(entries)
            raise

        try:
            self._require_source_tracked_state_matches_baseline(
                accepted_patch=accepted_patch,
                baseline_commit=baseline_commit,
                modified_files=unique_files,
            )
        except ValueError as exc:
            cleanup_failures = self._cleanup_promotion_entries(entries)
            status = "recovery_required" if cleanup_failures else "blocked"
            message = (
                "Source tracked state changed before promotion install; "
                + str(exc)
            )
            if cleanup_failures:
                message += "; stage cleanup requires recovery for: " + ", ".join(
                    cleanup_failures
                )
            journal["status"] = status
            journal["message"] = message
            journal["blocked_files"] = list(cleanup_failures)
            journal["entries"] = [item.to_journal_dict() for item in entries]
            self._write_promotion_journal(journal)
            return PromotionResult(
                status=status,
                message=message,
                blocked_files=list(cleanup_failures),
                journal_path=str(self.promotion_journal_path),
            )

        journal["entries"] = [entry.to_journal_dict() for entry in entries]
        journal["status"] = "prepared"
        try:
            self._write_promotion_journal(journal)
            journal["status"] = "applying"
            self._write_promotion_journal(journal)
        except Exception:
            self._cleanup_promotion_entries(entries)
            raise
        applied: list[str] = []
        failure: Exception | None = None
        failed_entry: _PromotionEntry | None = None
        try:
            for entry in entries:
                self._install_entry_cas(entry)
                entry.phase = "applied"
                applied.append(entry.rel_path)
                journal["applied_files"] = list(applied)
                journal["entries"] = [item.to_journal_dict() for item in entries]
                self._write_promotion_journal(journal)
            self._require_source_tracked_state_matches_baseline(
                accepted_patch=accepted_patch,
                baseline_commit=baseline_commit,
                modified_files=unique_files,
            )
            journal["status"] = "applied"
            journal["message"] = _APPLIED_PROMOTION_MESSAGE
            self._write_promotion_journal(journal)
        except Exception as exc:
            failure = exc
            failed_entry = entry

        if failure is not None:
            rollback_failures = self._rollback_promotion_entries(entries)
            cleanup_failures = self._cleanup_promotion_entries(entries)
            rollback_failures = list(
                dict.fromkeys(rollback_failures + cleanup_failures)
            )
            if rollback_failures:
                status = "recovery_required"
                message = (
                    f"Promotion failed ({failure}); automatic rollback needs "
                    "manual recovery for: " + ", ".join(rollback_failures)
                )
            elif isinstance(failure, _PromotionConflict):
                status = "blocked"
                message = str(failure)
            else:
                status = "rolled_back"
                message = f"Promotion failed and was rolled back: {failure}"

            journal["status"] = status
            journal["message"] = message
            journal["applied_files"] = []
            journal["blocked_files"] = (
                [failed_entry.rel_path]
                if isinstance(failure, _PromotionConflict) and failed_entry
                else []
            )
            journal["entries"] = [item.to_journal_dict() for item in entries]
            try:
                self._write_promotion_journal(journal)
            except Exception:
                # The source transaction has already been rolled back (or its
                # recovery paths retained).  Do not hide that metadata itself
                # could not be durably updated.
                raise failure
            return PromotionResult(
                status=status,
                message=message,
                blocked_files=list(journal["blocked_files"]),
                journal_path=str(self.promotion_journal_path),
            )

        cleanup_failures = self._cleanup_promotion_entries(entries)
        if cleanup_failures:
            journal["status"] = "recovery_required"
            journal["message"] = (
                "Promotion applied, but exact transaction cleanup requires "
                "manual recovery for: " + ", ".join(cleanup_failures)
            )
            journal["blocked_files"] = list(cleanup_failures)
            journal["entries"] = [item.to_journal_dict() for item in entries]
            self._write_promotion_journal(journal)
            return PromotionResult(
                status="recovery_required",
                message=journal["message"],
                blocked_files=list(cleanup_failures),
                journal_path=str(self.promotion_journal_path),
            )
        try:
            self._require_source_tracked_state_matches_baseline(
                accepted_patch=accepted_patch,
                baseline_commit=baseline_commit,
                modified_files=unique_files,
            )
        except ValueError as exc:
            journal["status"] = "recovery_required"
            journal["message"] = (
                "Promotion targets were installed, but final tracked-source "
                f"verification failed: {exc}"
            )
            journal["blocked_files"] = []
            journal["entries"] = [item.to_journal_dict() for item in entries]
            self._write_promotion_journal(journal)
            return PromotionResult(
                status="recovery_required",
                message=journal["message"],
                journal_path=str(self.promotion_journal_path),
            )
        return PromotionResult(
            status="applied",
            message=journal["message"],
            promoted_files=applied,
            journal_path=str(self.promotion_journal_path),
        )

    def read_file_from_commit(self, commit_hash: str, rel_path: str) -> bytes:
        """Read a file from a specific sandbox commit."""
        return self._git_output_bytes(["show", f"{commit_hash}:{rel_path}"], cwd=self.repo_root)

    def _source_target(self, rel_path: str) -> Path:
        if not rel_path or "\\" in rel_path:
            raise ValueError(f"Invalid promotion path: {rel_path!r}")
        pure_path = PurePosixPath(rel_path)
        if (
            pure_path.is_absolute()
            or pure_path.as_posix() != rel_path
            or any(part in {"", ".", ".."} for part in pure_path.parts)
        ):
            raise ValueError(f"Invalid promotion path: {rel_path!r}")

        target = self.source_project_root.joinpath(*pure_path.parts)
        current = self.source_project_root
        for part in pure_path.parts[:-1]:
            current /= part
            try:
                current_stat = os.lstat(current)
            except OSError as exc:
                raise ValueError(
                    f"Promotion parent is unavailable: {current}"
                ) from exc
            if stat_is_filesystem_alias(current_stat) or not stat.S_ISDIR(
                current_stat.st_mode
            ):
                raise ValueError(
                    f"Promotion parent is not a plain directory: {current}"
                )
        return target

    def _source_parent_chain_paths(self, rel_path: str) -> tuple[Path, ...]:
        self._source_target(rel_path)
        pure_path = PurePosixPath(rel_path)
        paths = [self.source_project_root]
        current = self.source_project_root
        for part in pure_path.parts[:-1]:
            current /= part
            paths.append(current)
        return tuple(paths)

    def _snapshot_source_parent_chain(
        self,
        rel_path: str,
    ) -> tuple[tuple[int, int], ...]:
        paths = self._source_parent_chain_paths(rel_path)
        snapshots: list[tuple[int, int]] = []
        for path in paths:
            try:
                current = os.lstat(path)
            except OSError as exc:
                raise _PromotionConflict(
                    f"Promotion parent is unavailable: {path}"
                ) from exc
            if stat_is_filesystem_alias(current) or not stat.S_ISDIR(
                current.st_mode
            ):
                raise _PromotionConflict(
                    f"Promotion parent is not a plain directory: {path}"
                )
            snapshots.append((current.st_dev, current.st_ino))

        for path, expected_identity in zip(paths, snapshots, strict=True):
            try:
                current = os.lstat(path)
            except OSError as exc:
                raise _PromotionConflict(
                    f"Promotion parent changed while binding: {path}"
                ) from exc
            if (
                stat_is_filesystem_alias(current)
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != expected_identity
            ):
                raise _PromotionConflict(
                    f"Promotion parent changed while binding: {path}"
                )
        return tuple(snapshots)

    def _validate_source_parent_chain(self, entry: _PromotionEntry) -> None:
        canonical_target = self.source_project_root.joinpath(
            *PurePosixPath(entry.rel_path).parts
        )
        if (
            entry.target != canonical_target
            or entry.stage.parent != canonical_target.parent
            or entry.backup.parent != canonical_target.parent
        ):
            raise _PromotionConflict(
                f"Promotion parent chain is not canonical: {entry.rel_path}"
            )
        try:
            current = self._snapshot_source_parent_chain(entry.rel_path)
        except (ValueError, _PromotionConflict) as exc:
            raise _PromotionConflict(
                f"Promotion parent chain changed: {entry.rel_path}"
            ) from exc
        if current != entry.parent_chain_identities:
            raise _PromotionConflict(
                f"Promotion parent chain changed: {entry.rel_path}"
            )

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _snapshot_regular_file(
        self,
        path: Path,
        *,
        expected_nlink: int = 1,
    ) -> _FileSnapshot:
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise _PromotionConflict(f"Source file is unavailable: {path}") from exc
        if (
            stat_is_filesystem_alias(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != expected_nlink
        ):
            link_contract = (
                "single-link"
                if expected_nlink == 1
                else f"{expected_nlink}-link"
            )
            raise _PromotionConflict(
                f"Source file is not a regular {link_contract} file: {path}"
            )

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise _PromotionConflict(f"Source file cannot be opened: {path}") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                stat_is_filesystem_alias(opened)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != expected_nlink
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                raise _PromotionConflict(
                    f"Source file changed while opening: {path}"
                )

            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after_open = os.fstat(descriptor)
            if (
                (after_open.st_dev, after_open.st_ino)
                != (opened.st_dev, opened.st_ino)
                or after_open.st_nlink != expected_nlink
                or after_open.st_size != opened.st_size
                or after_open.st_mode != opened.st_mode
                or after_open.st_mtime_ns != opened.st_mtime_ns
                or after_open.st_ctime_ns != opened.st_ctime_ns
            ):
                raise _PromotionConflict(
                    f"Source file changed while reading: {path}"
                )
        finally:
            os.close(descriptor)

        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise _PromotionConflict(
                f"Source file changed after reading: {path}"
            ) from exc
        if (
            stat_is_filesystem_alias(after_path)
            or not stat.S_ISREG(after_path.st_mode)
            or after_path.st_nlink != expected_nlink
            or (after_path.st_dev, after_path.st_ino)
            != (opened.st_dev, opened.st_ino)
            or after_path.st_size != opened.st_size
            or after_path.st_mode != opened.st_mode
            or after_path.st_mtime_ns != opened.st_mtime_ns
            or after_path.st_ctime_ns != opened.st_ctime_ns
        ):
            raise _PromotionConflict(f"Source file changed after reading: {path}")

        return _FileSnapshot(
            identity=(opened.st_dev, opened.st_ino),
            size=opened.st_size,
            mtime_ns=opened.st_mtime_ns,
            ctime_ns=opened.st_ctime_ns,
            mode=stat.S_IMODE(opened.st_mode),
            digest=self._sha256(b"".join(chunks)),
        )

    def _snapshot_installed_target(
        self,
        entry: _PromotionEntry,
    ) -> _FileSnapshot:
        self._validate_source_parent_chain(entry)
        if entry.installed_identity is None:
            raise _PromotionConflict(
                f"Installed target has no durable identity: {entry.rel_path}"
            )
        current = self._snapshot_regular_file(entry.target)
        if (
            current.identity != entry.installed_identity
            or current.digest != entry.accepted_digest
            or current.mode != entry.expected.mode
        ):
            raise _PromotionConflict(
                f"Installed target authority changed: {entry.rel_path}"
            )
        return current

    def _snapshot_interrupted_install_link(
        self,
        entry: _PromotionEntry,
    ) -> _FileSnapshot:
        """Authenticate the exact two-name inode left between link and unlink.

        ``_install_entry_cas`` installs without clobbering by linking the staged
        inode at the target name and then removing the stage name.  A process
        interruption between those two calls is legitimate only when the
        journal-owned names are the *only* two links to the same accepted-byte
        inode.  Every other hard-link shape remains fail-closed for manual
        recovery.
        """

        self._validate_source_parent_chain(entry)
        try:
            target_stat = os.lstat(entry.target)
            stage_stat = os.lstat(entry.stage)
        except OSError as exc:
            raise _PromotionConflict(
                f"Interrupted install paths are unavailable: {entry.rel_path}"
            ) from exc
        target_identity = (target_stat.st_dev, target_stat.st_ino)
        stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
        if (
            stat_is_filesystem_alias(target_stat)
            or stat_is_filesystem_alias(stage_stat)
            or not stat.S_ISREG(target_stat.st_mode)
            or not stat.S_ISREG(stage_stat.st_mode)
            or target_identity != stage_identity
            or entry.stage_identity is None
            or target_identity != entry.stage_identity
            or target_stat.st_nlink != 2
            or stage_stat.st_nlink != 2
            or stat.S_IMODE(target_stat.st_mode) != entry.expected.mode
            or stat.S_IMODE(stage_stat.st_mode) != entry.expected.mode
        ):
            raise _PromotionConflict(
                "Interrupted install is not the exact journal-owned hard-link "
                f"pair: {entry.rel_path}"
            )

        linked = self._snapshot_regular_file(
            entry.target,
            expected_nlink=2,
        )
        try:
            stage_after = os.lstat(entry.stage)
        except OSError as exc:
            raise _PromotionConflict(
                f"Interrupted install stage changed: {entry.rel_path}"
            ) from exc
        if (
            (stage_after.st_dev, stage_after.st_ino) != linked.identity
            or stage_after.st_nlink != 2
            or linked.digest != entry.accepted_digest
            or linked.mode != entry.expected.mode
        ):
            raise _PromotionConflict(
                f"Interrupted install bytes changed: {entry.rel_path}"
            )

        return linked

    def _collapse_interrupted_install_link(
        self,
        entry: _PromotionEntry,
    ) -> _FileSnapshot:
        """Remove the redundant stage name after exact pair authentication."""

        linked = self._snapshot_interrupted_install_link(entry)
        self._validate_source_parent_chain(entry)
        entry.stage.unlink()
        installed = self._snapshot_regular_file(entry.target)
        if (
            installed.identity != linked.identity
            or installed.digest != entry.accepted_digest
            or installed.mode != entry.expected.mode
        ):
            raise _PromotionConflict(
                f"Interrupted install target changed: {entry.rel_path}"
            )
        return installed

    def _write_exclusive_stage(
        self,
        entry: _PromotionEntry,
        content: bytes,
    ) -> _FileSnapshot:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        self._validate_source_parent_chain(entry)
        descriptor = os.open(entry.stage, flags, entry.expected.mode)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(
                        f"Failed to stage promotion bytes: {entry.stage}"
                    )
                view = view[written:]
            os.fchmod(descriptor, entry.expected.mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        staged = self._snapshot_regular_file(entry.stage)
        if (
            staged.digest != entry.accepted_digest
            or staged.mode != entry.expected.mode
        ):
            raise _PromotionConflict(
                f"Staged promotion bytes or mode changed: {entry.rel_path}"
            )
        entry.stage_identity = staged.identity
        return staged

    def _install_entry_cas(self, entry: _PromotionEntry) -> None:
        staged = self._snapshot_regular_file(entry.stage)
        if (
            entry.stage_identity is None
            or staged.identity != entry.stage_identity
            or staged.digest != entry.accepted_digest
            or staged.mode != entry.expected.mode
        ):
            raise _PromotionConflict(
                f"Promotion stage authority changed: {entry.rel_path}"
            )
        if os.path.lexists(entry.backup):
            raise _PromotionConflict(
                f"Promotion backup path already exists: {entry.backup}"
            )

        entry.phase = "capturing"
        try:
            self._validate_source_parent_chain(entry)
            os.replace(entry.target, entry.backup)
        except OSError as exc:
            raise _PromotionConflict(
                f"Source file changed before atomic capture: {entry.rel_path}"
            ) from exc

        try:
            captured = self._snapshot_regular_file(entry.backup)
            if (
                captured.identity != entry.expected.identity
                or captured.digest != entry.expected.digest
                or captured.mode != entry.expected.mode
            ):
                raise _PromotionConflict(
                    "Source file changed after promotion preflight: "
                    f"{entry.rel_path}"
                )
            if os.path.lexists(entry.target):
                raise _PromotionConflict(
                    "Source path was recreated during promotion: "
                    f"{entry.rel_path}"
                )
            self._validate_source_parent_chain(entry)
            try:
                os.link(entry.stage, entry.target, follow_symlinks=False)
            except TypeError:
                self._validate_source_parent_chain(entry)
                os.link(entry.stage, entry.target)
            self._validate_source_parent_chain(entry)
            entry.stage.unlink()
            installed = self._snapshot_regular_file(entry.target)
            if (
                installed.identity != entry.stage_identity
                or installed.digest != entry.accepted_digest
                or installed.mode != entry.expected.mode
            ):
                raise _PromotionConflict(
                    "Installed source file does not match accepted bytes: "
                    f"{entry.rel_path}"
                )
            entry.installed_identity = installed.identity
            entry.phase = "installed"
        except Exception:
            if not os.path.lexists(entry.target) and os.path.lexists(entry.backup):
                try:
                    self._validate_source_parent_chain(entry)
                    os.replace(entry.backup, entry.target)
                except (OSError, _PromotionConflict):
                    entry.phase = "recovery_required"
                else:
                    entry.phase = "restored"
            else:
                entry.phase = "recovery_required"
            raise

    def _rollback_promotion_entries(
        self,
        entries: list[_PromotionEntry],
    ) -> list[str]:
        failures: list[str] = []
        for entry in reversed(entries):
            if entry.phase == "recovery_required":
                failures.append(entry.rel_path)
                continue
            if entry.phase in {"prepared", "restored"}:
                entry.phase = "restored"
                continue
            if not os.path.lexists(entry.backup):
                failures.append(entry.rel_path)
                entry.phase = "recovery_required"
                continue

            try:
                backup_snapshot = self._snapshot_regular_file(entry.backup)
                if (
                    backup_snapshot.identity != entry.expected.identity
                    or backup_snapshot.digest != entry.expected.digest
                    or backup_snapshot.mode != entry.expected.mode
                ):
                    raise _PromotionConflict(
                        "Promotion backup changed before rollback: "
                        f"{entry.rel_path}"
                    )
                if not os.path.lexists(entry.target):
                    self._validate_source_parent_chain(entry)
                    os.replace(entry.backup, entry.target)
                    entry.phase = "restored"
                    continue

                if os.path.lexists(entry.stage):
                    raise _PromotionConflict(
                        f"Rollback capture path already exists: {entry.stage}"
                    )
                self._validate_source_parent_chain(entry)
                os.replace(entry.target, entry.stage)
                captured = self._snapshot_regular_file(entry.stage)
                if (
                    captured.digest != entry.accepted_digest
                    or captured.mode != entry.expected.mode
                    or (
                        entry.installed_identity is not None
                        and captured.identity != entry.installed_identity
                    )
                ):
                    self._validate_source_parent_chain(entry)
                    os.replace(entry.stage, entry.target)
                    raise _PromotionConflict(
                        "Promoted source file changed before rollback: "
                        f"{entry.rel_path}"
                    )
                self._validate_source_parent_chain(entry)
                os.replace(entry.backup, entry.target)
                self._validate_source_parent_chain(entry)
                entry.stage.unlink(missing_ok=True)
                entry.phase = "restored"
            except Exception as exc:
                logger.error("Failed to roll back %s: %s", entry.rel_path, exc)
                if (
                    not os.path.lexists(entry.target)
                    and os.path.lexists(entry.stage)
                ):
                    try:
                        self._validate_source_parent_chain(entry)
                        os.replace(entry.stage, entry.target)
                    except (OSError, _PromotionConflict):
                        pass
                failures.append(entry.rel_path)
                entry.phase = "recovery_required"
        return failures

    def _recover_interrupted_promotion(
        self,
        accepted_patch: AcceptedPatchRecord,
    ) -> PromotionResult | None:
        payload = self._read_promotion_journal()
        active_statuses = {
            "preparing",
            "prepared",
            "applying",
            "rolling_back",
            "recovery_required",
        }
        status = payload.get("status") if payload is not None else None
        if payload is None or status not in active_statuses:
            return None

        # Recovery authority is the same exact durable accepted chain as a new
        # promotion.  The mutable output journal can describe transaction
        # progress, but it can never independently authorize source mutation.
        accepted_commit, baseline_commit, unique_files = (
            self._accepted_state_for_promotion(accepted_patch)
        )
        expected_keys = (
            _INTERRUPTED_JOURNAL_KEYS | {"message"}
            if status == "recovery_required"
            else _INTERRUPTED_JOURNAL_KEYS
        )
        if set(payload) != expected_keys or payload.get("schema_version") != 2:
            raise RuntimeError("Interrupted promotion journal has an unknown schema.")

        transaction_id = payload.get("transaction_id")
        raw_entries = payload.get("entries")
        applied_files = payload.get("applied_files")
        blocked_files = payload.get("blocked_files")
        if (
            not isinstance(transaction_id, str)
            or len(transaction_id) != 32
            or any(char not in "0123456789abcdef" for char in transaction_id)
            or payload.get("source_project_root")
            != str(self.source_project_root)
            or payload.get("sandbox_repo") != str(self.repo_root)
            or payload.get("baseline_commit") != baseline_commit
            or payload.get("accepted_commit") != accepted_commit
            or payload.get("files") != unique_files
            or not isinstance(raw_entries, list)
            or not isinstance(applied_files, list)
            or applied_files != unique_files[: len(applied_files)]
            or not isinstance(blocked_files, list)
            or not all(
                isinstance(item, str) and item in unique_files
                for item in blocked_files
            )
            or len(blocked_files) != len(set(blocked_files))
            or not isinstance(payload.get("timestamp"), str)
            or not payload["timestamp"]
            or (
                status == "recovery_required"
                and (
                    not isinstance(payload.get("message"), str)
                    or not payload["message"]
                )
            )
        ):
            raise RuntimeError("Interrupted promotion journal identity is invalid.")
        if not raw_entries:
            if status != "preparing" or applied_files:
                raise RuntimeError("Interrupted promotion journal entries are missing.")
            payload["status"] = "rolled_back"
            payload["message"] = "Recovered an interruption before staging began."
            payload["blocked_files"] = []
            self._write_promotion_journal(payload)
            return None
        if len(raw_entries) != len(unique_files):
            raise RuntimeError("Interrupted promotion journal entries are incomplete.")

        entries: list[_PromotionEntry] = []
        installed_identities: list[tuple[int, int] | None] = []
        allowed_phases = {
            "staging",
            "prepared",
            "capturing",
            "installed",
            "applied",
            "restored",
            "recovery_required",
        }
        for index, (rel_path, raw_entry) in enumerate(
            zip(unique_files, raw_entries, strict=True)
        ):
            if (
                not isinstance(raw_entry, dict)
                or set(raw_entry) != _PROMOTION_ENTRY_KEYS
            ):
                raise RuntimeError("Interrupted promotion entry is malformed.")
            expected_identity = self._journal_identity(
                raw_entry.get("expected_identity")
            )
            expected_mode = self._journal_mode(raw_entry.get("expected_mode"))
            parent_chain_identities = self._journal_parent_chain(
                raw_entry.get("parent_chain_identities"),
                expected_length=len(PurePosixPath(rel_path).parts),
            )
            stage_identity_value = raw_entry.get("stage_identity")
            stage_identity = (
                None
                if stage_identity_value is None
                else self._journal_identity(stage_identity_value)
            )
            installed_identity_value = raw_entry.get("installed_identity")
            installed_identity = (
                None
                if installed_identity_value is None
                else self._journal_identity(installed_identity_value)
            )
            try:
                baseline_git_mode = self._regular_file_mode_from_commit(
                    baseline_commit,
                    rel_path,
                )
                accepted_git_mode = self._regular_file_mode_from_commit(
                    accepted_commit,
                    rel_path,
                )
            except ValueError as exc:
                raise RuntimeError(
                    "Interrupted promotion entry is malformed."
                ) from exc
            expected_digest = self._sha256(
                self.read_file_from_commit(baseline_commit, rel_path)
            )
            accepted_digest = self._sha256(
                self.read_file_from_commit(accepted_commit, rel_path)
            )
            if (
                raw_entry.get("path") != rel_path
                or expected_identity is None
                or expected_mode is None
                or parent_chain_identities is None
                or accepted_git_mode != baseline_git_mode
                or bool(expected_mode & 0o111)
                != bool(baseline_git_mode & 0o111)
                or (
                    stage_identity_value is not None
                    and stage_identity is None
                )
                or (
                    installed_identity_value is not None
                    and installed_identity is None
                )
                or raw_entry.get("expected_digest") != expected_digest
                or raw_entry.get("accepted_digest") != accepted_digest
                or raw_entry.get("phase") not in allowed_phases
                or (
                    raw_entry.get("phase") in {"installed", "applied"}
                    and (
                        stage_identity is None
                        or installed_identity is None
                        or stage_identity != installed_identity
                    )
                )
                or (
                    raw_entry.get("phase") == "prepared"
                    and stage_identity is None
                )
            ):
                raise RuntimeError("Interrupted promotion entry is malformed.")

            target = self.source_project_root.joinpath(
                *PurePosixPath(rel_path).parts
            )
            stage = target.with_name(
                f".{target.name}.omicsclaw-{transaction_id}-{index}.new"
            )
            backup = target.with_name(
                f".{target.name}.omicsclaw-{transaction_id}-{index}.bak"
            )
            if (
                raw_entry.get("stage_name") != stage.name
                or raw_entry.get("backup_name") != backup.name
            ):
                raise RuntimeError("Interrupted promotion paths are not canonical.")

            entry = _PromotionEntry(
                rel_path=rel_path,
                target=target,
                stage=stage,
                backup=backup,
                expected=_FileSnapshot(
                    identity=expected_identity,
                    size=0,
                    mtime_ns=0,
                    ctime_ns=0,
                    mode=expected_mode,
                    digest=expected_digest,
                ),
                accepted_digest=accepted_digest,
                parent_chain_identities=parent_chain_identities,
                stage_identity=stage_identity,
                installed_identity=installed_identity,
            )
            try:
                self._validate_source_parent_chain(entry)
            except _PromotionConflict:
                entry.phase = "recovery_required"
            entries.append(entry)
            installed_identities.append(installed_identity)

        try:
            self._require_source_tracked_state_matches_baseline(
                accepted_patch=accepted_patch,
                baseline_commit=baseline_commit,
                modified_files=unique_files,
            )
        except ValueError as exc:
            payload["status"] = "recovery_required"
            payload["message"] = (
                "Interrupted promotion recovery is blocked by tracked-source "
                f"drift: {exc}"
            )
            payload["blocked_files"] = []
            self._write_promotion_journal(payload)
            return PromotionResult(
                status="recovery_required",
                message=payload["message"],
                journal_path=str(self.promotion_journal_path),
            )

        # Only after the complete journal has been authenticated against Git do
        # we inspect the source transaction.  Inspection remains read-only;
        # exact link-pair collapse and rollback happen in the following phase.
        linked_entries: list[tuple[_PromotionEntry, tuple[int, int]]] = []
        for entry, recorded_installed_identity in zip(
            entries,
            installed_identities,
            strict=True,
        ):
            if entry.phase == "recovery_required":
                continue
            target = entry.target
            stage = entry.stage
            backup = entry.backup
            if os.path.lexists(backup):
                try:
                    backup_snapshot = self._snapshot_regular_file(backup)
                except _PromotionConflict:
                    entry.phase = "recovery_required"
                    continue
                if (
                    backup_snapshot.identity != entry.expected.identity
                    or backup_snapshot.digest != entry.expected.digest
                    or backup_snapshot.mode != entry.expected.mode
                ):
                    entry.phase = "recovery_required"
                    continue
                if os.path.lexists(target):
                    try:
                        current = (
                            self._snapshot_interrupted_install_link(entry)
                            if os.path.lexists(stage)
                            else self._snapshot_regular_file(target)
                        )
                    except _PromotionConflict:
                        entry.phase = "recovery_required"
                    else:
                        if (
                            current.digest == entry.accepted_digest
                            and current.mode == entry.expected.mode
                            and entry.stage_identity is not None
                            and current.identity == entry.stage_identity
                            and (
                                recorded_installed_identity is None
                                or current.identity
                                == recorded_installed_identity
                            )
                        ):
                            entry.phase = "applied"
                            entry.installed_identity = current.identity
                            if os.path.lexists(stage):
                                linked_entries.append((entry, current.identity))
                        else:
                            entry.phase = "recovery_required"
                else:
                    entry.phase = "capturing"
            elif os.path.lexists(stage):
                try:
                    current_target = self._snapshot_regular_file(target)
                    current_stage = self._snapshot_regular_file(stage)
                except _PromotionConflict:
                    entry.phase = "recovery_required"
                else:
                    entry.phase = (
                        "prepared"
                        if current_target.identity == entry.expected.identity
                        and current_target.digest == entry.expected.digest
                        and current_target.mode == entry.expected.mode
                        and entry.stage_identity is not None
                        and current_stage.identity == entry.stage_identity
                        and current_stage.digest == entry.accepted_digest
                        and current_stage.mode == entry.expected.mode
                        else "recovery_required"
                    )
            else:
                try:
                    current = self._snapshot_regular_file(target)
                except _PromotionConflict:
                    entry.phase = "recovery_required"
                else:
                    entry.phase = (
                        "restored"
                        if current.identity == entry.expected.identity
                        and current.digest == entry.expected.digest
                        and current.mode == entry.expected.mode
                        else "recovery_required"
                    )

        for entry, linked_identity in linked_entries:
            try:
                installed = self._collapse_interrupted_install_link(entry)
            except _PromotionConflict:
                entry.phase = "recovery_required"
            else:
                if installed.identity != linked_identity:
                    entry.phase = "recovery_required"
                else:
                    entry.installed_identity = installed.identity

        failures = self._rollback_promotion_entries(entries)
        cleanup_failures = self._cleanup_promotion_entries(entries)
        failures = list(dict.fromkeys(failures + cleanup_failures))
        if failures:
            payload["status"] = "recovery_required"
            payload["message"] = (
                "Interrupted promotion needs manual recovery for: "
                + ", ".join(failures)
            )
            payload["blocked_files"] = list(failures)
            payload["entries"] = [entry.to_journal_dict() for entry in entries]
            self._write_promotion_journal(payload)
            return PromotionResult(
                status="recovery_required",
                message=str(payload["message"]),
                journal_path=str(self.promotion_journal_path),
            )

        payload["status"] = "rolled_back"
        payload["message"] = "Recovered and rolled back an interrupted promotion."
        payload["applied_files"] = []
        payload["blocked_files"] = []
        payload["entries"] = [entry.to_journal_dict() for entry in entries]
        self._write_promotion_journal(payload)
        return None

    def _read_promotion_journal(self) -> dict[str, Any] | None:
        if not os.path.lexists(self.promotion_journal_path):
            return None
        if not is_scientific_output_file(
            self.promotion_journal_path,
            output_root=self.output_root,
        ):
            raise RuntimeError("Promotion journal is not an owned regular file.")
        try:
            if self.promotion_journal_path.stat().st_size > 1024 * 1024:
                raise RuntimeError("Promotion journal exceeds the size limit.")
            payload = json.loads(
                self.promotion_journal_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Promotion journal cannot be read.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Promotion journal must contain an object.")
        return payload

    def _cleanup_promotion_entries(
        self,
        entries: list[_PromotionEntry],
    ) -> list[str]:
        failures: list[str] = []
        applied_entries: list[_PromotionEntry] = []
        for entry in entries:
            if entry.phase == "recovery_required":
                # Both names may be the only remaining copies of user and
                # accepted bytes.  Leave them intact for journal-led recovery.
                failures.append(entry.rel_path)
                continue
            try:
                self._validate_source_parent_chain(entry)
            except _PromotionConflict as exc:
                logger.warning(
                    "Failed to authenticate promotion parent chain %s: %s",
                    entry.target.parent,
                    exc,
                )
                entry.phase = "recovery_required"
                failures.append(entry.rel_path)
                continue
            if entry.phase == "applied":
                try:
                    self._snapshot_installed_target(entry)
                except _PromotionConflict as exc:
                    logger.warning(
                        "Failed to authenticate applied promotion target %s: %s",
                        entry.target,
                        exc,
                    )
                    entry.phase = "recovery_required"
                    failures.append(entry.rel_path)
                    continue
                applied_entries.append(entry)
            try:
                if os.path.lexists(entry.stage):
                    if entry.phase == "applied":
                        linked = self._snapshot_interrupted_install_link(entry)
                        if (
                            entry.installed_identity is None
                            or linked.identity != entry.installed_identity
                        ):
                            raise _PromotionConflict(
                                "Applied promotion stage identity changed: "
                                f"{entry.rel_path}"
                            )
                    else:
                        linked = None
                        staged = self._snapshot_regular_file(entry.stage)
                        if (
                            entry.stage_identity is None
                            or staged.identity != entry.stage_identity
                            or staged.digest != entry.accepted_digest
                            or staged.mode != entry.expected.mode
                        ):
                            raise _PromotionConflict(
                                "Promotion stage bytes changed before cleanup: "
                                f"{entry.rel_path}"
                            )
                    self._validate_source_parent_chain(entry)
                    entry.stage.unlink()
                    if linked is not None:
                        collapsed = self._snapshot_installed_target(entry)
                        if collapsed.identity != linked.identity:
                            raise _PromotionConflict(
                                "Applied promotion target changed during cleanup: "
                                f"{entry.rel_path}"
                            )
            except (OSError, _PromotionConflict) as exc:
                logger.warning("Failed to clean promotion stage %s: %s", entry.stage, exc)
                entry.phase = "recovery_required"
                failures.append(entry.rel_path)
                continue
            try:
                if os.path.lexists(entry.backup):
                    if entry.phase == "applied":
                        self._snapshot_installed_target(entry)
                    backup = self._snapshot_regular_file(entry.backup)
                    if (
                        backup.identity != entry.expected.identity
                        or backup.digest != entry.expected.digest
                        or backup.mode != entry.expected.mode
                    ):
                        raise _PromotionConflict(
                            "Promotion backup changed before cleanup: "
                            f"{entry.rel_path}"
                        )
                    self._validate_source_parent_chain(entry)
                    entry.backup.unlink()
            except (OSError, _PromotionConflict) as exc:
                logger.warning(
                    "Failed to clean promotion backup %s: %s", entry.backup, exc
                )
                entry.phase = "recovery_required"
                failures.append(entry.rel_path)

        # Re-authenticate every promoted target after all private names have
        # been processed.  A cleanup result is never reported as applied from
        # an earlier snapshot alone.
        for entry in applied_entries:
            if entry.phase == "recovery_required":
                continue
            try:
                self._snapshot_installed_target(entry)
            except _PromotionConflict as exc:
                logger.warning(
                    "Applied promotion target changed after cleanup %s: %s",
                    entry.target,
                    exc,
                )
                entry.phase = "recovery_required"
                failures.append(entry.rel_path)
        return list(dict.fromkeys(failures))

    def baseline_code_state(self) -> dict[str, Any]:
        """Metadata that identifies the immutable baseline snapshot."""
        return {
            "kind": "baseline_snapshot",
            "sandbox_repo": str(self.repo_root),
            "sandbox_commit": self.baseline_commit,
            "source_project_commit": self.source_project_commit,
        }

    def accepted_head_state(self) -> dict[str, Any]:
        """Metadata for the latest accepted sandbox commit."""
        return {
            "kind": "accepted_head",
            "sandbox_repo": str(self.repo_root),
            "sandbox_commit": self.accepted_commit,
            "source_project_commit": self.source_project_commit,
        }

    def _capture_tracked_source_snapshot(
        self,
        *,
        unobserved_paths: set[str] | None = None,
    ) -> tuple[str, bytes, list[_TrackedSourceEntry]]:
        """Freeze stage-zero paths and all non-transaction source bytes."""

        unobserved = set() if unobserved_paths is None else set(unobserved_paths)

        try:
            top_level = Path(
                self._git_output(
                    ["rev-parse", "--path-format=absolute", "--show-toplevel"],
                    cwd=self.source_project_root,
                )
            ).resolve()
            source_commit = self._git_output(
                ["rev-parse", "HEAD^{commit}"],
                cwd=self.source_project_root,
            )
            object_format = self._git_output(
                ["rev-parse", "--show-object-format"],
                cwd=self.source_project_root,
            )
            raw_index = self._git_output_bytes(
                ["ls-files", "--cached", "--stage", "-z"],
                cwd=self.source_project_root,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                "AutoAgent source_project_root must be a readable Git top-level "
                "with a committed HEAD."
            ) from exc
        if top_level != self.source_project_root:
            raise ValueError(
                "AutoAgent source_project_root must equal the source Git top-level."
            )
        if (
            object_format not in {"sha1", "sha256"}
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit)
            is None
        ):
            raise ValueError("Source Git object identity is unsupported.")
        expected_oid_length = hashlib.new(object_format).digest_size * 2
        records = raw_index.split(b"\0")
        if records and records[-1] == b"":
            records.pop()
        if not records:
            raise ValueError("Source Git index has no tracked stage-zero files.")

        entries: list[_TrackedSourceEntry] = []
        index_objects: list[tuple[str, str]] = []
        seen: set[str] = set()
        for record in records:
            if b"\t" not in record:
                raise ValueError("Source Git index entry is malformed.")
            metadata, raw_path = record.split(b"\t", 1)
            fields = metadata.split()
            try:
                index_mode = fields[0].decode("ascii")
                oid = fields[1].decode("ascii")
                stage = fields[2].decode("ascii")
                rel_path = raw_path.decode("utf-8")
            except (IndexError, UnicodeDecodeError) as exc:
                raise ValueError("Source Git index is not canonical UTF-8.") from exc
            rel_path = self._canonical_repo_path(
                rel_path,
                label="source tracked path",
            )
            if any(ord(char) < 32 or ord(char) == 127 for char in rel_path):
                raise ValueError("Source tracked path contains a control character.")
            if (
                len(fields) != 3
                or stage != "0"
                or index_mode not in {"100644", "100755"}
                or len(oid) != expected_oid_length
                or set(oid) == {"0"}
                or any(char not in "0123456789abcdef" for char in oid)
                or rel_path in seen
            ):
                raise ValueError(
                    "Source Git index contains an unresolved or unsupported entry."
                )
            seen.add(rel_path)
            index_objects.append((rel_path, oid))
            source_path = self.source_project_root.joinpath(
                *PurePosixPath(rel_path).parts
            )
            try:
                source_path.relative_to(self.output_root)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "Tracked source path overlaps the AutoAgent output root."
                )
            if first_filesystem_alias_component(source_path.parent) is not None:
                raise ValueError("Source tracked path has an aliased parent.")
            if rel_path in unobserved:
                continue

            try:
                before = self._snapshot_regular_file(source_path)
                content = self._read_stable_regular_bytes(source_path)
                after = self._snapshot_regular_file(source_path)
            except (_PromotionConflict, OSError, ValueError) as exc:
                raise ValueError(
                    f"Tracked source file cannot be read stably: {rel_path}"
                ) from exc
            if before != after or before.mode & 0o7000:
                raise ValueError(
                    f"Tracked source file changed or has special mode bits: {rel_path}"
                )
            git_mode = "100755" if before.mode & 0o111 else "100644"
            source_state = self._raw_regular_entry(before)
            entries.append(
                _TrackedSourceEntry(
                    rel_path=rel_path,
                    git_mode=git_mode,
                    content=content,
                    source_state=source_state,
                )
            )
        checked_objects = self._git_output_bytes_with_input(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=self.source_project_root,
            input_bytes=b"".join(
                oid.encode("ascii") + b"\n" for _rel_path, oid in index_objects
            ),
        ).splitlines()
        expected_objects = [
            f"{oid} blob".encode("ascii") for _rel_path, oid in index_objects
        ]
        if checked_objects != expected_objects:
            raise ValueError(
                "Source Git index object is missing or is not a regular Git blob."
            )
        if not unobserved.issubset(seen):
            raise ValueError("Required promotion target is absent from source Git.")
        return source_commit, raw_index, entries

    def _verify_tracked_source_snapshot(
        self,
        *,
        source_project_commit: str,
        source_index: bytes,
        expected_entries: list[_TrackedSourceEntry],
    ) -> None:
        current_commit, current_index, current_entries = (
            self._capture_tracked_source_snapshot()
        )
        if (
            current_commit != source_project_commit
            or current_index != source_index
            or current_entries != expected_entries
        ):
            raise ValueError("Source tracked state changed while creating the baseline.")

    def _copy_project_snapshot(
        self,
        entries: list[_TrackedSourceEntry],
    ) -> None:
        self.repo_root.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            destination = self.repo_root.joinpath(
                *PurePosixPath(entry.rel_path).parts
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(entry.content)
            destination.chmod(entry.source_state.mode)
            copied = self._snapshot_regular_file(destination)
            if (
                copied.digest != self._sha256(entry.content)
                or copied.mode != entry.source_state.mode
            ):
                raise ValueError(
                    f"Tracked source snapshot copy is inconsistent: {entry.rel_path}"
                )

    def _init_git_repo(self, entries: list[_TrackedSourceEntry]) -> None:
        self._git(["init"], cwd=self.repo_root)
        self._git(["config", "user.name", "OmicsClaw Harness"], cwd=self.repo_root)
        self._git(["config", "user.email", "omicsclaw-autoagent@local"], cwd=self.repo_root)
        self._git(["config", "core.autocrlf", "false"], cwd=self.repo_root)
        self._git(["config", "core.safecrlf", "false"], cwd=self.repo_root)
        info_dir = self.repo_root / ".git" / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        attributes = info_dir / "attributes"
        attributes.write_text(
            "* -text -eol -crlf -filter -ident -working-tree-encoding\n",
            encoding="utf-8",
        )
        attributes.chmod(0o600)

        index_records: list[bytes] = []
        expected_blobs: dict[str, tuple[str, str]] = {}
        for entry in entries:
            raw_oid = self._git_output_bytes_with_input(
                ["hash-object", "-w", "--stdin", "--no-filters"],
                cwd=self.repo_root,
                input_bytes=entry.content,
            ).strip()
            try:
                oid = raw_oid.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("Sandbox Git returned a non-ASCII object ID.") from exc
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None:
                raise ValueError("Sandbox Git returned an invalid object ID.")
            index_records.append(
                f"{entry.git_mode} blob {oid}\t{entry.rel_path}".encode("utf-8")
                + b"\0"
            )
            expected_blobs[entry.rel_path] = (entry.git_mode, oid)
        self._git_output_bytes_with_input(
            ["update-index", "-z", "--index-info"],
            cwd=self.repo_root,
            input_bytes=b"".join(index_records),
        )
        tree_hash = self._git_output(["write-tree"], cwd=self.repo_root)
        commit_hash = self._git_output_with_input(
            ["commit-tree", tree_hash],
            cwd=self.repo_root,
            input_text="Harness baseline snapshot\n",
        )
        self._git(["update-ref", "HEAD", commit_hash], cwd=self.repo_root)
        object_format = self._git_output(
            ["rev-parse", "--show-object-format"],
            cwd=self.repo_root,
        )
        if self._commit_blob_inventory(
            commit_hash,
            object_format=object_format,
        ) != expected_blobs:
            raise ValueError("Synthetic baseline Git tree does not match tracked bytes.")

    def _write_promotion_journal(self, payload: dict[str, Any]) -> None:
        atomic_write_owned_output_text(
            self.promotion_journal_path,
            output_root=self.output_root,
            text=json.dumps(payload, indent=2, default=str),
            label="promotion journal",
        )

    def _remove_owned_metadata_file(self, path: Path) -> None:
        try:
            if is_scientific_output_file(path, output_root=self.output_root):
                path.unlink()
        except OSError as exc:
            logger.warning("Failed to clean partial harness metadata %s: %s", path, exc)

    @staticmethod
    def _reset_path(path: Path) -> None:
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        path.mkdir(parents=True, exist_ok=True)

    def _authenticate_git_command_cwd(self, cwd: Path) -> None:
        """Authenticate Git storage and any linked-worktree command context."""

        self._require_git_control_uncompromised()
        if not self._created:
            if self._rebuilding:
                return
            raise RuntimeError(
                "HarnessWorkspace.create() or open_existing() must be called "
                "before Git access."
            )
        self._authenticate_sandbox_git_authority()
        candidate = Path(os.path.abspath(cwd))
        try:
            relative = candidate.relative_to(self.worktrees_root)
        except ValueError:
            return
        if len(relative.parts) == 1 and os.path.lexists(candidate / ".git"):
            self._authenticate_linked_worktree_git_authority(candidate)

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self._authenticate_git_command_cwd(cwd)
        cmd = ["git", *args]
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=self._git_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
        if check and proc.returncode != 0:
            stderr = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {stderr}")
        return proc

    def _git_output(self, args: list[str], *, cwd: Path) -> str:
        proc = self._git(args, cwd=cwd)
        return proc.stdout.strip()

    def _git_output_with_input(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_text: str,
    ) -> str:
        self._authenticate_git_command_cwd(cwd)
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=self._git_environment(),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {stderr}")
        return proc.stdout.strip()

    def _git_output_bytes(self, args: list[str], *, cwd: Path) -> bytes:
        self._authenticate_git_command_cwd(cwd)
        cmd = ["git", *args]
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=self._git_environment(),
            capture_output=True,
            text=False,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {stderr}")
        return proc.stdout

    def _git_output_bytes_with_input(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_bytes: bytes,
    ) -> bytes:
        self._authenticate_git_command_cwd(cwd)
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=self._git_environment(),
            input=input_bytes,
            capture_output=True,
            text=False,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {stderr}")
        return proc.stdout

    @staticmethod
    def _git_environment() -> dict[str, str]:
        """Return a deterministic Git environment without caller control vars."""

        environment = {
            key: value
            for key, value in scrub_internal_control_credentials(os.environ).items()
            if not key.startswith("GIT_")
        }
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        return environment
